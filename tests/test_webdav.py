# -*- coding: utf-8 -*-
"""WebDAV 备份端到端测试：内嵌一个最小 WebDAV 服务器（PUT/MKCOL/DELETE/PROPFIND + Basic 认证）。"""
import base64
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config as cfgmod
from src.webdav import BackupWorker, Client, WebDavError

AUTH = "Basic " + base64.b64encode(b"user:pass").decode("ascii")


class MockDavHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockDav/1.0"

    # 共享存储挂在 server 实例上
    @property
    def store(self):
        return self.server.store

    def _key(self):
        return urllib.parse.unquote(self.path).lstrip("/")

    def _check_auth(self):
        if self.headers.get("Authorization") != AUTH:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="dav"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        return True

    def _reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, *args):
        pass

    def do_PROPFIND(self):
        if not self._check_auth():
            return
        k = self._key()
        if k in self.store["files"] or k in self.store["dirs"] or k == "":
            self._reply(207, b"<?xml version='1.0'?><multistatus/>")
        else:
            self._reply(404)

    def do_MKCOL(self):
        if not self._check_auth():
            return
        k = self._key()
        if k in self.store["dirs"] or k in self.store["files"]:
            self._reply(405)
            return
        self.store["dirs"].add(k)
        self._reply(201)

    def do_PUT(self):
        if not self._check_auth():
            return
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        k = self._key()
        self.store["files"][k] = body
        self.store["dirs"].add(os.path.dirname(k))
        self.server.put_count += 1
        self._reply(201)

    def do_DELETE(self):
        if not self._check_auth():
            return
        k = self._key()
        if k in self.store["files"]:
            del self.store["files"][k]
            self.server.delete_count += 1
            self._reply(204)
        else:
            self._reply(404)


def make_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockDavHandler)
    srv.store = {"files": {}, "dirs": set()}
    srv.put_count = 0
    srv.delete_count = 0
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class WebDavBackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gss-wdav-")
        self.share = os.path.join(self.tmp, "saves")
        os.makedirs(self.share)
        self.state_path = os.path.join(self.tmp, "webdav_state.json")
        self.srv = make_server()
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write(self, rel, data, mtime=None):
        p = os.path.join(self.share, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def _cfg(self, **over):
        raw = {
            "shares": [{"name": "test", "path": self.share}],
            "interval": 3,
            "delete_mirror": False,
            "webdav_enabled": True,
            "webdav_url": f"http://127.0.0.1:{self.srv.server_port}/dav",
            "webdav_user": "user",
            "webdav_pass": "pass",
            "webdav_dir": "GameSaveSync",
        }
        raw.update(over)
        return cfgmod.Config(raw)

    def _worker(self, cfg):
        logs = []
        w = BackupWorker(cfg, logs.append, state_path=self.state_path)
        return w, logs

    def test_upload_skip_and_change(self):
        self._write("a.sav", b"AAA")
        self._write("sub/b.sav", b"BBB", mtime=1000.0)
        w, _ = self._worker(self._cfg())

        w.run_once()
        base = f"dav/GameSaveSync/test/"
        self.assertEqual(self.srv.store["files"][base + "a.sav"], b"AAA")
        self.assertEqual(self.srv.store["files"][base + "sub/b.sav"], b"BBB")
        self.assertEqual(self.srv.put_count, 2)

        # 无变化：不再上传
        w.run_once()
        self.assertEqual(self.srv.put_count, 2)

        # 内容变化（大小+时间都变）：重新上传
        self._write("a.sav", b"AAAA-CHANGED", mtime=time.time() + 10)
        w.run_once()
        self.assertEqual(self.srv.put_count, 3)
        self.assertEqual(self.srv.store["files"][base + "a.sav"], b"AAAA-CHANGED")

    def test_delete_mirror(self):
        self._write("gone.sav", b"bye")
        cfg = self._cfg(delete_mirror=True)
        w, _ = self._worker(cfg)
        w.run_once()
        key = "dav/GameSaveSync/test/gone.sav"
        self.assertIn(key, self.srv.store["files"])

        os.remove(os.path.join(self.share, "gone.sav"))
        w.run_once()
        self.assertNotIn(key, self.srv.store["files"])
        self.assertEqual(self.srv.delete_count, 1)

    def test_no_delete_by_default(self):
        self._write("keep.sav", b"data")
        w, _ = self._worker(self._cfg())
        w.run_once()
        os.remove(os.path.join(self.share, "keep.sav"))
        w.run_once()
        self.assertIn("dav/GameSaveSync/test/keep.sav", self.srv.store["files"])
        self.assertEqual(self.srv.delete_count, 0)

    def test_bad_auth_raises(self):
        c = Client(f"http://127.0.0.1:{self.srv.server_port}/dav", "user", "wrong")
        with self.assertRaises(WebDavError):
            c.exists(["dav", "x"])

    def test_disabled_noop(self):
        self._write("a.sav", b"AAA")
        w, _ = self._worker(self._cfg(webdav_enabled=False))
        w.run_once()
        self.assertEqual(self.srv.put_count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
