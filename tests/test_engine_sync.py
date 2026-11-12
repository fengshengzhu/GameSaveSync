# -*- coding: utf-8 -*-
"""本机回环上的双引擎同步测试：握手、推/拉、被动方删除传播。"""
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config as cfgmod
from src.engine import SyncEngine


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class EngineSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gss-sync-")
        self.dir_a = os.path.join(self.tmp, "a")
        self.dir_b = os.path.join(self.tmp, "b")
        os.makedirs(self.dir_a)
        os.makedirs(self.dir_b)
        self.port = free_port()
        self.logs = []
        self._engines = []

    def tearDown(self):
        for e in self._engines:
            try:
                e.stop()
            except Exception:
                pass
        shutil.rmtree(self.tmp, True)

    def _engine(self, name, path, delete_mirror):
        cfg = cfgmod.Config({
            "device_name": name,
            "token": "TEST-TOKEN",
            "port": self.port,
            "shares": [{"name": "save", "path": path}],
            "auto_sync": False,
            "interval": 3,
            "delete_mirror": delete_mirror,
        })
        eng = SyncEngine(cfg, self.logs.append)
        self._engines.append(eng)
        return eng

    def _write(self, path, data, mtime=None):
        with open(path, "wb") as f:
            f.write(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))

    def _wait_for(self, path, timeout=5.0):
        """推送是异步落盘的：主动方 sendall 返回时，被动方可能还没写完。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(path):
                return
            time.sleep(0.02)
        self.fail(f"等待文件超时：{path}（日志：{self.logs}）")

    def test_push_pull_and_passive_delete(self):
        a = self._engine("peerA", self.dir_a, delete_mirror=True)
        b = self._engine("peerB", self.dir_b, delete_mirror=True)
        b.start()

        f_a = os.path.join(self.dir_a, "shared.sav")
        f_b = os.path.join(self.dir_b, "shared.sav")
        self._write(f_a, b"A1", mtime=time.time())

        # 握手 + 首轮：A 主动连接，把本地文件推给 B
        self.assertTrue(a.connect(f"127.0.0.1:{self.port}"), self.logs)
        self._wait_for(f_b)
        with open(f_b, "rb") as f:
            self.assertEqual(f.read(), b"A1")

        # B 修改文件（时间更新），A 下一轮应拉取
        self._write(f_b, b"B2-B2-B2", mtime=time.time() + 10)
        a.run_sync_once("peerB")
        with open(f_a, "rb") as f:
            self.assertEqual(f.read(), b"B2-B2-B2")

        # 被动方 B 删除，主动方 A 下一轮应收到删除通知并一起删除
        os.remove(f_b)
        a.run_sync_once("peerB")
        self.assertFalse(os.path.exists(f_a), self.logs)
        self.assertFalse(os.path.exists(f_b))

    def test_stop_start_restores_background_services(self):
        """停止服务再启动后，发现与 WebDAV 线程必须仍然存活。"""
        b = self._engine("peerB", self.dir_b, delete_mirror=False)
        b.start()
        b.stop()
        b.start()

        self.assertFalse(b.discovery._stop.is_set())
        self.assertTrue(any(t.is_alive() for t in b.discovery._threads))
        self.assertFalse(b.webdav._stop.is_set())
        self.assertTrue(b.webdav._thread.is_alive())

        # TCP 监听也应能重新接受连接
        a = self._engine("peerA", self.dir_a, delete_mirror=False)
        f_a = os.path.join(self.dir_a, "after-restart.sav")
        f_b = os.path.join(self.dir_b, "after-restart.sav")
        self._write(f_a, b"RESTART", mtime=time.time())
        self.assertTrue(a.connect(f"127.0.0.1:{self.port}"), self.logs)
        self._wait_for(f_b)
        with open(f_b, "rb") as f:
            self.assertEqual(f.read(), b"RESTART")

    def test_delete_ignored_when_local_option_off(self):
        """一端关闭删除镜像时，另一端发来的删除应被忽略，文件从对端恢复。"""
        a = self._engine("peerA", self.dir_a, delete_mirror=True)
        b = self._engine("peerB", self.dir_b, delete_mirror=False)
        b.start()

        f_a = os.path.join(self.dir_a, "shared.sav")
        f_b = os.path.join(self.dir_b, "shared.sav")
        self._write(f_a, b"KEEP", mtime=time.time())
        self.assertTrue(a.connect(f"127.0.0.1:{self.port}"), self.logs)
        self._wait_for(f_b)
        with open(f_b, "rb") as f:
            self.assertEqual(f.read(), b"KEEP")

        # B 本机删除：B 未开启删除镜像，因此不会发 delete；A 发现 B 缺文件后推回去
        os.remove(f_b)
        a.run_sync_once("peerB")
        self._wait_for(f_b)
        with open(f_b, "rb") as f:
            self.assertEqual(f.read(), b"KEEP")
        self.assertTrue(os.path.exists(f_a))


if __name__ == "__main__":
    unittest.main(verbosity=2)
