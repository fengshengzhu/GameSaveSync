# -*- coding: utf-8 -*-
"""WebDAV 云盘备份（Cloudreve / 坚果云 / Nextcloud 等）。

纯标准库实现：
  - Client: PROPFIND / MKCOL / PUT / GET / DELETE + Basic 认证，URL 逐段编码
  - BackupWorker: 后台线程按间隔比对本地扫描与已上传状态，只上传有变化的文件；
    delete_mirror 开启时联动删除远端文件；支持手动从云端恢复到本地。
    状态持久化在 webdav_state.json。

远端目录结构：<webdav_url>/<webdav_dir>/<共享夹名>/<相对路径>
"""
import base64
import email.utils
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from . import config as cfgmod
from . import protocol as P

UA = "GameSaveSync/1.0"
SMALL = 16 * 1024 * 1024  # 小于该值的文件整体读入内存上传，更大的走流式


class WebDavError(Exception):
    pass


class Client:
    """极简 WebDAV 客户端，只做备份与恢复需要的几种操作。"""

    def __init__(self, base_url: str, username: str, password: str, insecure: bool = False):
        self.base = base_url.rstrip("/")
        if not self.base:
            raise WebDavError("WebDAV 地址为空")
        cred = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self.auth = "Basic " + cred
        # insecure=True：跳过证书校验（自签名证书，或证书链里有过期交叉证书导致
        # Windows/Python 证书库验证失败）。TLS 加密仍然生效，只是不校验服务器身份。
        # 明文 HTTP 地址不需要上下文，避免无谓地枚举系统证书库。
        if not self.base.lower().startswith("https"):
            self._context = None
        elif insecure:
            self._context = ssl._create_unverified_context()
        else:
            self._context = ssl.create_default_context()

    def _url(self, segments):
        """segments: 已按 '/' 拆分的相对路径列表，逐段百分号编码。"""
        quoted = "/".join(urllib.parse.quote(s) for s in segments)
        return f"{self.base}/{quoted}" if quoted else self.base

    def request(self, method: str, segments, data=None, headers=None, timeout=60):
        req = urllib.request.Request(self._url(segments), data=data, method=method)
        req.add_header("Authorization", self.auth)
        req.add_header("User-Agent", UA)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=self._context)
        except urllib.error.HTTPError as e:
            # 4xx/5xx 交由调用方按状态码处理
            return e
        except (urllib.error.URLError, OSError) as e:
            raise WebDavError(f"{method} 请求失败：{e}")

    # ---------- 备份所需操作 ----------
    def exists(self, segments) -> bool:
        r = self.request("PROPFIND", segments, headers={"Depth": "0"})
        code = r.status
        r.close()
        if code in (207, 200):
            return True
        if code == 404:
            return False
        raise WebDavError(f"PROPFIND 返回 {code}")

    def mkdir(self, segments) -> bool:
        r = self.request("MKCOL", segments)
        code = r.status
        r.close()
        if code in (201, 200):
            return True
        if code in (405, 301):  # 已存在 / 集合形式不同
            return False
        raise WebDavError(f"MKCOL 返回 {code}")

    def ensure_dirs(self, segments):
        """逐级创建目录，已存在则跳过。"""
        for i in range(1, len(segments) + 1):
            self.mkdir(segments[:i])

    def put_file(self, local_path: str, size: int, segments):
        """上传文件。小文件整体读入；大文件流式 + 显式 Content-Length。"""
        if size <= SMALL:
            with open(local_path, "rb") as f:
                data = f.read()
            r = self.request("PUT", segments, data=data,
                             headers={"Content-Type": "application/octet-stream"},
                             timeout=300)
        else:
            f = open(local_path, "rb")
            try:
                r = self.request("PUT", segments, data=f,
                                 headers={"Content-Type": "application/octet-stream",
                                          "Content-Length": str(size)},
                                 timeout=300)
            finally:
                f.close()
        code = r.status
        r.close()
        if code not in (200, 201, 204):
            raise WebDavError(f"PUT 返回 {code}")

    def delete(self, segments) -> bool:
        r = self.request("DELETE", segments)
        code = r.status
        r.close()
        if code in (200, 204):
            return True
        if code == 404:
            return False
        raise WebDavError(f"DELETE 返回 {code}")

    # ---------- 恢复所需操作 ----------
    def get_file(self, segments, dest_path: str):
        """下载文件到 dest_path（先写 .part 再原子替换），并按服务器时间戳设置 mtime。
        返回 (size, mtime)。"""
        r = self.request("GET", segments, timeout=300)
        try:
            if r.status != 200:
                raise WebDavError(f"GET 返回 {r.status}")
            lm = r.headers.get("Last-Modified")
            try:
                mtime = email.utils.parsedate_to_datetime(lm).timestamp() if lm else time.time()
            except (TypeError, ValueError):
                mtime = time.time()
            tmp = dest_path + ".part"
            os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
            size = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    size += len(chunk)
            os.replace(tmp, dest_path)
            os.utime(dest_path, (mtime, mtime))
            return size, mtime
        finally:
            r.close()

    def list_dir(self, segments):
        """PROPFIND Depth:1 列出目录的直接子项，返回 [(name, is_dir, size, mtime)]。
        name 为相对该目录的名称；size 对目录为 0。"""
        r = self.request("PROPFIND", segments, headers={"Depth": "1"})
        try:
            if r.status == 404:
                raise WebDavError("云端目录不存在")
            if r.status not in (207, 200):
                raise WebDavError(f"PROPFIND 返回 {r.status}")
            body = r.read()
        finally:
            r.close()
        # 前缀必须同样做百分号解码：中文/空格目录名在 URL 里是编码形式，
        # 若拿编码后的前缀去比解码后的 href，目录自身会被误当成子项。
        prefix = urllib.parse.unquote(urllib.parse.urlsplit(self._url(segments)).path).rstrip("/")
        out = []
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            raise WebDavError(f"PROPFIND 响应解析失败：{e}")
        # 注意：Element.iter() 不支持 {*} 通配符，必须用 findall
        for resp in root.findall(".//{*}response"):
            href_el = resp.find("{*}href")
            if href_el is None or not href_el.text:
                continue
            path = urllib.parse.unquote(urllib.parse.urlsplit(href_el.text).path)
            if path.rstrip("/") == prefix:
                continue  # 目录自身
            name = path.rstrip("/")
            if name.startswith(prefix):
                name = name[len(prefix):].lstrip("/")
            else:
                name = name.rsplit("/", 1)[-1]
            if not name or "/" in name:
                continue  # 服务器返回了超出 Depth:1 的条目
            is_dir = resp.find(".//{*}collection") is not None
            size = 0
            if not is_dir:
                size_el = resp.find(".//{*}getcontentlength")
                if size_el is not None and size_el.text and size_el.text.isdigit():
                    size = int(size_el.text)
            mtime = 0.0
            lm_el = resp.find(".//{*}getlastmodified")
            if lm_el is not None and lm_el.text:
                try:
                    mtime = email.utils.parsedate_to_datetime(lm_el.text).timestamp()
                except (TypeError, ValueError):
                    mtime = 0.0
            out.append((name, is_dir, size, mtime))
        return out


class BackupWorker:
    """定时把共享夹备份到 WebDAV。与 P2P 同步相互独立，可单独使用。"""

    def __init__(self, cfg: "cfgmod.Config", log, state_path: str = None):
        self.cfg = cfg
        self.log = log
        self._stop = threading.Event()
        self._thread = None
        self._busy = threading.Lock()
        self.state_path = state_path or os.path.join(cfgmod.APP_DIR, "webdav_state.json")
        self._state = self._load_state()
        self._last_scan = {}  # 会话内的上次扫描，用于检测删除

    # ---------- 生命周期 ----------
    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="GameSaveSync-WebDAV")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(max(3, self.cfg.interval)):
            if not self.cfg.webdav_enabled:
                continue
            try:
                self.run_once()
            except WebDavError as e:
                self.log(f"[WebDAV] {e}")
            except Exception as e:
                self.log(f"[WebDAV] 备份出错：{type(e).__name__}: {e}")

    # ---------- 状态 ----------
    def _load_state(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    # ---------- 备份逻辑 ----------
    def _targets(self):
        names = {s["name"] for s in self.cfg.shares}
        want = {n for n in self.cfg.webdav_shares if n in names}
        return sorted(want or names)

    def run_once(self, manual=False):
        if not self.cfg.webdav_enabled:
            if manual:
                self.log("[WebDAV] 未启用：先在界面上勾选并保存设置")
            return
        if not self.cfg.shares:
            if manual:
                self.log("[WebDAV] 没有共享夹可备份")
            return
        if not self.cfg.webdav_url or not self.cfg.webdav_user:
            if manual:
                self.log("[WebDAV] 请先填写服务器地址和用户名并保存")
            return
        with self._busy:
            self._run_locked(manual)

    def _run_locked(self, manual=False):
        client = Client(self.cfg.webdav_url, self.cfg.webdav_user,
                        self.cfg.webdav_pass, insecure=self.cfg.webdav_insecure)
        base = [s for s in self.cfg.webdav_dir.split("/") if s]
        for share in self._targets():
            root = self.cfg.share_path(share)
            if not root or not os.path.isdir(root):
                continue
            scan = P.list_share(root)
            state = self._state.setdefault(share, {})
            prev = self._last_scan.get(share)
            uploaded = 0

            # 删除镜像：本机会话内删除 -> 删除远端文件
            if self.cfg.delete_mirror and prev is not None:
                for rel in list(state):
                    if rel not in scan and rel in prev:
                        try:
                            client.delete(base + [share] + rel.split("/"))
                            state.pop(rel)
                            self.log(f"[WebDAV] 删除远端 {share}/{rel}")
                        except WebDavError as e:
                            self.log(f"[WebDAV] 删除 {share}/{rel} 失败：{e}")

            # 确保远端共享夹目录存在
            try:
                client.ensure_dirs(base + [share])
            except WebDavError as e:
                self.log(f"[WebDAV] 创建目录失败：{e}")
                return

            for rel in sorted(scan):
                meta = scan[rel]
                st = state.get(rel)
                if st and st.get("size") == meta["size"] \
                        and abs(st.get("mtime", 0) - meta["mtime"]) < 0.5:
                    continue  # 与上次上传完全一致
                segments = base + [share] + rel.split("/")
                parent = segments[:-1]
                try:
                    client.ensure_dirs(parent)
                    client.put_file(os.path.join(root, rel.replace("/", os.sep)),
                                    meta["size"], segments)
                    state[rel] = {"size": meta["size"], "mtime": meta["mtime"]}
                    uploaded += 1
                    self.log(f"[WebDAV] 已备份 {share}/{rel}")
                except (WebDavError, OSError) as e:
                    self.log(f"[WebDAV] 上传 {share}/{rel} 失败：{e}")

            self._last_scan[share] = scan
            self._save_state()
            if uploaded == 0 and manual:
                self.log(f"[WebDAV] {share}：云端已是最新")
        if manual:
            self.log("[WebDAV] 备份完成")

    # ---------- 恢复 ----------
    def _cloud_walk(self, client, base, share) -> dict:
        """递归遍历云端共享夹，返回 {rel: {"size","mtime"}}；rel 用 '/' 分隔。"""
        out = {}

        def walk(segs, rel_prefix):
            try:
                items = client.list_dir(segs)
            except WebDavError as e:
                self.log(f"[WebDAV] 列出云端 {share}/{rel_prefix} 失败：{e}")
                return
            for name, is_dir, size, mtime in items:
                rel = f"{rel_prefix}/{name}" if rel_prefix else name
                if is_dir:
                    walk(segs + [name], rel)
                else:
                    out[rel] = {"size": size, "mtime": mtime}

        walk(base + [share], "")
        return out

    def restore(self, share: str, mode: str = "missing") -> int:
        """从云端恢复共享夹到本地，返回恢复的文件数。
        mode: missing=仅补齐本地缺失的文件；overwrite=云端覆盖本地所有文件。
        恢复按云端的 Last-Modified 还原修改时间，随后更新本地状态缓存。"""
        root = self.cfg.share_path(share)
        if not root:
            raise WebDavError(f"共享夹 {share} 未配置本地路径")
        with self._busy:  # 与自动备份互斥，避免并发读写状态
            return self._restore_locked(share, mode, root)

    def _restore_locked(self, share: str, mode: str, root: str) -> int:
        client = Client(self.cfg.webdav_url, self.cfg.webdav_user,
                        self.cfg.webdav_pass, insecure=self.cfg.webdav_insecure)
        base = [s for s in self.cfg.webdav_dir.split("/") if s]
        cloud = self._cloud_walk(client, base, share)
        if not cloud:
            self.log(f"[WebDAV] {share}：云端没有可恢复的文件")
            return 0
        os.makedirs(root, exist_ok=True)

        done = 0
        downloaded = []
        for rel in sorted(cloud):
            meta = cloud[rel]
            dest = os.path.join(root, rel.replace("/", os.sep))
            if os.path.isfile(dest):
                if mode == "missing":
                    continue  # 本地已有：补齐模式下不动
                if mode == "overwrite" \
                        and os.path.getsize(dest) == meta["size"] \
                        and meta["mtime"] \
                        and abs(os.path.getmtime(dest) - meta["mtime"]) < 0.5:
                    continue  # 与云端完全一致，跳过
            try:
                client.get_file(base + [share] + rel.split("/"), dest)
                done += 1
                downloaded.append(rel)
                self.log(f"[WebDAV] 已恢复 {share}/{rel}")
            except (WebDavError, OSError) as e:
                self.log(f"[WebDAV] 恢复 {share}/{rel} 失败：{e}")

        # 恢复下来的文件记入上传状态，避免下一轮备份把刚下载的内容再传一遍
        scan = P.list_share(root)
        state = self._state.setdefault(share, {})
        for rel in downloaded:
            if rel in scan:
                state[rel] = {"size": scan[rel]["size"], "mtime": scan[rel]["mtime"]}
        self._last_scan[share] = scan  # 刷新会话扫描，防止误判删除
        self._save_state()
        self.log(f"[WebDAV] {share}：恢复完成，共 {done} 个文件")
        return done
