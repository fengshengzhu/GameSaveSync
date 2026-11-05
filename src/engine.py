# -*- coding: utf-8 -*-
"""同步引擎：双栈(TCP v4/v6)监听、配对握手、清单交换、双向 LWW 传输、定时自动同步。"""
import os
import socket
import threading
import time

from . import config as cfgmod
from . import protocol as P
from .discovery import Discovery


def parse_target(text: str):
    """解析对端地址为 (host, port, family_hint)。
    支持: 192.168.1.5 / 192.168.1.5:29876 / [240e:xxxx::1] / [240e:xxxx::1]:29876 / 裸IPv6。
    """
    text = text.strip()
    port = None
    host = text
    if text.startswith("["):
        end = text.find("]")
        host = text[1:end]
        rest = text[end + 1:]
        if rest.startswith(":"):
            port = int(rest[1:])
    elif ":" in text and text.count(":") == 1:
        host, p = text.rsplit(":", 1)
        port = int(p)
    # 含冒号即 IPv6，否则 IPv4
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return host, port or cfgmod.DEFAULT_PORT, family


class SyncEngine:
    def __init__(self, cfg: cfgmod.Config, log):
        self.cfg = cfg
        self.log = log
        self.discovery = Discovery(cfg, log)
        self._stop = threading.Event()
        self._listener = None
        self._conns = {}  # peer_name -> {"sock", "addr", "lock", "shares":set}
        self._conn_lock = threading.Lock()
        self._last_scan = {}  # (peer_name, share) -> 上次同步轮的本地清单，用于检测本机删除
        self.running = False

    # ---------------- 生命周期 ----------------
    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        self._listener.bind(("::", self.cfg.port))
        self._listener.listen(16)
        self.running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._auto_loop, daemon=True).start()
        self.discovery.start()
        self.log(f"服务已启动，监听端口 {self.cfg.port}（同时接受 IPv4 与 IPv6）")

    def stop(self):
        self._stop.set()
        self.discovery.stop()
        try:
            self._listener and self._listener.close()
        except OSError:
            pass
        with self._conn_lock:
            for info in self._conns.values():
                try:
                    info["sock"].close()
                except OSError:
                    pass
            self._conns.clear()
        self.running = False

    # ---------------- 接受连接（服务端一侧）----------------
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._listener.accept()
            except OSError:
                break
            threading.Thread(target=self._server_handshake, args=(conn, addr), daemon=True).start()

    def _server_handshake(self, conn: socket.socket, addr):
        try:
            conn.settimeout(10)
            nonce = os.urandom(16).hex()
            P.send_msg(conn, {"type": "challenge", "nonce": nonce})
            hello = P.recv_msg(conn)
            if hello.get("type") != "hello":
                P.send_msg(conn, {"type": "hello_ack", "ok": False, "reason": "协议错误"})
                return
            expect = P.mac(self.cfg.token, nonce)
            if not hmac_compare(hello.get("mac", ""), expect):
                P.send_msg(conn, {"type": "hello_ack", "ok": False, "reason": "配对码不匹配"})
                self.log(f"拒绝来自 {addr[0]} 的连接：配对码错误")
                return
            peer_name = hello.get("name", addr[0])
            local_shares = {s["name"] for s in self.cfg.shares}
            P.send_msg(conn, {"type": "hello_ack", "ok": True, "name": self.cfg.device_name,
                              "shares": sorted(local_shares)})
            info = self._register(conn, peer_name, addr, set(hello.get("shares", [])))
            self.log(f"已连接：{peer_name}（{addr[0]}）")
            self._read_loop(conn, peer_name, info)
        except (ConnectionError, OSError, ValueError) as e:
            self.log(f"连接 {addr[0]} 结束：{e}")
        finally:
            self._unregister_by_sock(conn)
            try:
                conn.close()
            except OSError:
                pass

    # ---------------- 主动连接（客户端一侧）----------------
    def connect(self, target: str):
        host, port, family = parse_target(target)
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(10)
        try:
            sock.connect((host, port))
        except (OSError, ValueError) as e:
            self.log(f"连接 {target} 失败：{e}")
            return False
        try:
            ch = P.recv_msg(sock)
            if ch.get("type") != "challenge":
                raise ConnectionError("对端未返回握手挑战")
            local_shares = {s["name"] for s in self.cfg.shares}
            P.send_msg(sock, {
                "type": "hello",
                "name": self.cfg.device_name,
                "mac": P.mac(self.cfg.token, ch["nonce"]),
                "shares": sorted(local_shares),
            })
            ack = P.recv_msg(sock)
            if not ack.get("ok"):
                self.log(f"被 {host} 拒绝：{ack.get('reason', '未知原因')}")
                return False
            peer_name = ack.get("name", host)
            info = self._register(sock, peer_name, (host, port), set(ack.get("shares", [])), initiator=True)
            sock.settimeout(None)
            self.log(f"已连接：{peer_name}（{host}），共享夹：{', '.join(ack.get('shares', [])) or '无'}")
            # 发起方不另起读线程：同步轮在本线程内一问一答，避免同一条 socket 上两个读者抢消息
            self.run_sync_once(peer_name)
            return True
        except (ConnectionError, OSError, ValueError) as e:
            self.log(f"握手失败：{e}")
            try:
                sock.close()
            except OSError:
                pass
            return False

    # ---------------- 连接登记 ----------------
    def _register(self, sock, peer_name, addr, peer_shares: set, initiator: bool = False):
        self._set_keepalive(sock)
        with self._conn_lock:
            # 同名旧连接替换
            old = self._conns.get(peer_name)
            if old:
                try:
                    old["sock"].close()
                except OSError:
                    pass
            info = {
                "sock": sock, "addr": addr, "lock": threading.Lock(),
                "shares": peer_shares, "initiator": initiator,
            }
            self._conns[peer_name] = info
            return info

    def _unregister_by_sock(self, sock):
        with self._conn_lock:
            for name, info in list(self._conns.items()):
                if info["sock"] is sock:
                    self._conns.pop(name, None)
                    self.log(f"连接断开：{name}")

    @staticmethod
    def _set_keepalive(sock):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass

    # ---------------- 读取循环：响应对端的 push/pull/manifest ----------------
    def _read_loop(self, sock: socket.socket, peer_name: str, info: dict):
        sock.settimeout(None)
        lock = info["lock"]
        while not self._stop.is_set():
            try:
                msg = P.recv_msg(sock)
            except (ConnectionError, OSError, ValueError):
                break
            mtype = msg.get("type")
            with lock:  # 与本地同步轮互斥，避免消息流交错
                try:
                    if mtype == "push":
                        self._handle_push(sock, msg, peer_name)
                    elif mtype == "pull":
                        self._handle_pull(sock, msg, peer_name)
                    elif mtype == "manifest":
                        self._reply_manifest(sock, msg)
                    elif mtype == "sig_req":
                        self._handle_sig_req(sock, msg)
                    elif mtype == "delete":
                        self._handle_delete(sock, msg, peer_name)
                    elif mtype == "ping":
                        P.send_msg(sock, {"type": "pong"})
                except (ConnectionError, OSError, ValueError) as e:
                    self.log(f"[{peer_name}] 处理消息出错：{type(e).__name__}: {e}")
                    break
        self._unregister_by_sock(sock)

    def _reply_manifest(self, sock, msg):
        share = msg.get("share")
        root = self._share_root(share)
        files = P.list_share(root) if root and os.path.isdir(root) else {}
        P.send_msg(sock, {"type": "manifest", "share": share, "files": files})

    def _share_root(self, share: str):
        return self.cfg.share_path(share)

    @staticmethod
    def _safe_rel(rel: str) -> bool:
        return ".." not in rel.replace("\\", "/").split("/") and not os.path.isabs(rel)

    def _handle_push(self, sock, msg, peer_name):
        share, rel = msg.get("share"), msg.get("rel")
        root = self._share_root(share)
        if not root or not self._safe_rel(rel):
            P.send_msg(sock, {"type": "error", "reason": "无效共享夹或路径"})
            return
        dest = os.path.join(root, rel.replace("/", os.sep))
        try:
            if msg.get("delta"):
                data = P.recv_exact(sock, int(msg["dsize"]))
                block = int(msg["block"])
                size = int(msg["size"])
                off = 0
                pieces = {}
                for i in msg["changed"]:
                    bl = min(block, size - int(i) * block)
                    pieces[int(i)] = data[off:off + bl]
                    off += bl
                P.apply_delta(dest, size, pieces, block)
            else:
                P.recv_file_into(sock, int(msg["size"]), dest)
            os.utime(dest, (msg["mtime"], msg["mtime"]))
            self.log(f"[{peer_name}] 收到 {share}/{rel}")
        except (OSError, ConnectionError) as e:
            self.log(f"接收 {rel} 失败：{e}")

    def _handle_pull(self, sock, msg, peer_name):
        share, rel = msg.get("share"), msg.get("rel")
        root = self._share_root(share)
        full = os.path.join(root, rel.replace("/", os.sep)) if root else None
        if not full or not self._safe_rel(rel) or not os.path.isfile(full):
            P.send_msg(sock, {"type": "error", "reason": "本地无此文件"})
            return
        try:
            st = os.stat(full)
            # 增量拉取：对端给出了它本地旧文件的分块签名，只发送有变化的块
            basis = msg.get("basis")
            if basis is not None and st.st_size > P.DELTA_THRESHOLD:
                changed = P.changed_blocks(full, st.st_size, basis)
                data = b"".join(d for _, d in changed)
                if 0 < len(data) < st.st_size:
                    P.send_msg(sock, {"type": "file", "share": share, "rel": rel,
                                      "size": st.st_size, "mtime": st.st_mtime,
                                      "delta": 1, "block": P.BLOCK,
                                      "changed": [i for i, _ in changed], "dsize": len(data)})
                    sock.sendall(data)
                    return
            P.send_msg(sock, {"type": "file", "share": share, "rel": rel,
                              "size": st.st_size, "mtime": st.st_mtime})
            P.send_file(sock, full, st.st_size)
        except (OSError, ConnectionError) as e:
            self.log(f"发送 {rel} 失败：{e}")

    def _handle_sig_req(self, sock, msg):
        """对端索取某文件（它本地的旧版本）的分块签名，用于计算增量。"""
        share, rel = msg.get("share"), msg.get("rel")
        root = self._share_root(share)
        full = os.path.join(root, rel.replace("/", os.sep)) if root else None
        blocks = []
        if full and self._safe_rel(rel) and os.path.isfile(full):
            try:
                blocks = P.block_hashes(full)
            except OSError:
                blocks = []
        P.send_msg(sock, {"type": "sig", "share": share, "rel": rel, "blocks": blocks})

    def _handle_delete(self, sock, msg, peer_name):
        share, rel = msg.get("share"), msg.get("rel")
        root = self._share_root(share)
        full = os.path.join(root, rel.replace("/", os.sep)) if root else None
        if not full or not self._safe_rel(rel):
            return
        try:
            if os.path.isfile(full):
                os.remove(full)
                self.log(f"[{peer_name}] 删除镜像 {share}/{rel}")
                # 顺手清理变空的父目录（不越过共享夹根）
                parent = os.path.dirname(full)
                root_abs = os.path.abspath(root)
                while os.path.abspath(parent).startswith(root_abs) and \
                        os.path.abspath(parent) != root_abs:
                    try:
                        os.rmdir(parent)
                    except OSError:
                        break
                    parent = os.path.dirname(parent)
        except OSError as e:
            self.log(f"删除 {rel} 失败：{e}")

    # ---------------- 同步轮 ----------------
    def connected_peers(self) -> list:
        with self._conn_lock:
            return list(self._conns.keys())

    def run_sync_once(self, peer_name: str = None):
        with self._conn_lock:
            if peer_name is None:
                targets = [(n, i) for n, i in self._conns.items() if i.get("initiator")]
            elif peer_name in self._conns and self._conns[peer_name].get("initiator"):
                targets = [(peer_name, self._conns[peer_name])]
            else:
                targets = []
        for name, info in targets:
            with info["lock"]:
                try:
                    self._sync_round(name, info)
                except (ConnectionError, OSError, ValueError) as e:
                    self.log(f"与 {name} 的连接已断开：{e}")
                    self._teardown_initiator(name, info)
                except Exception as e:  # 其他异常不拖垮连接，下轮重试
                    self.log(f"与 {name} 同步轮出错：{e}")

    def _teardown_initiator(self, name: str, info: dict):
        try:
            info["sock"].close()
        except OSError:
            pass
        with self._conn_lock:
            if self._conns.get(name) is info:
                self._conns.pop(name, None)

    def _sync_round(self, peer_name: str, info: dict):
        sock = info["sock"]
        common = {s["name"] for s in self.cfg.shares} & info["shares"]
        if not common:
            return
        for share in sorted(common):
            root = self._share_root(share)
            local = P.list_share(root) if root and os.path.isdir(root) else {}
            # 删除镜像：对比上次扫描，检测本机会话内发生的删除并传播给对端
            last = self._last_scan.get((peer_name, share))
            if self.cfg.delete_mirror and last is not None:
                for rel in last:
                    if rel not in local:
                        P.send_msg(sock, {"type": "delete", "share": share, "rel": rel})
                        self.log(f"[{peer_name}] 传播删除 {share}/{rel}")
            self._last_scan[(peer_name, share)] = local
            P.send_msg(sock, {"type": "manifest", "share": share, "files": local})
            remote_manifest = self._recv_manifest(sock, share)
            plan = P.decide(local, remote_manifest)
            for rel in plan["push"]:
                full = os.path.join(root, rel.replace("/", os.sep))
                st = os.stat(full)
                if self._push_delta(sock, peer_name, share, rel, full, st,
                                    rel in remote_manifest):
                    continue
                P.send_msg(sock, {"type": "push", "share": share, "rel": rel,
                                  "size": st.st_size, "mtime": st.st_mtime})
                P.send_file(sock, full, st.st_size)
                self.log(f"[{peer_name}] 推送 {share}/{rel}")
            for rel in plan["pull"]:
                full = os.path.join(root, rel.replace("/", os.sep))
                self._pull_file(sock, peer_name, share, rel, full)

    def _push_delta(self, sock, peer_name, share, rel, full, st, remote_has: bool) -> bool:
        """大文件推送前先取对端旧文件的分块签名，只传差异块。返回是否已按增量发送。"""
        if not remote_has or st.st_size <= P.DELTA_THRESHOLD:
            return False
        try:
            P.send_msg(sock, {"type": "sig_req", "share": share, "rel": rel})
            blocks = self._recv_sig(sock, share, rel)
            if not blocks:
                return False
            changed = P.changed_blocks(full, st.st_size, blocks)
            data = b"".join(d for _, d in changed)
            if not (0 < len(data) < st.st_size):
                return False  # 差异太多或不完整，退回全量
            P.send_msg(sock, {"type": "push", "share": share, "rel": rel,
                              "size": st.st_size, "mtime": st.st_mtime,
                              "delta": 1, "block": P.BLOCK,
                              "changed": [i for i, _ in changed], "dsize": len(data)})
            sock.sendall(data)
            self.log(f"[{peer_name}] 推送 {share}/{rel}（增量 {len(data)}/{st.st_size} 字节）")
            return True
        except (ConnectionError, OSError, ValueError):
            raise
        except Exception as e:
            self.log(f"[{peer_name}] {rel} 增量失败改用全量：{e}")
            return False

    def _pull_file(self, sock, peer_name, share, rel, full):
        """拉取对端文件；本地已有旧版本且够大时用增量。"""
        msg = {"type": "pull", "share": share, "rel": rel}
        try:
            if os.path.isfile(full) and os.path.getsize(full) > P.DELTA_THRESHOLD:
                basis = P.block_hashes(full)
                msg["basis"] = basis
        except OSError:
            pass
        P.send_msg(sock, msg)
        resp = P.recv_msg(sock)
        if resp.get("type") != "file":
            self.log(f"[{peer_name}] 拉取 {rel} 失败：{resp.get('reason')}")
            return
        try:
            if resp.get("delta"):
                data = P.recv_exact(sock, int(resp["dsize"]))
                block = int(resp["block"])
                size = int(resp["size"])
                off = 0
                pieces = {}
                for i in resp["changed"]:
                    bl = min(block, size - int(i) * block)
                    pieces[int(i)] = data[off:off + bl]
                    off += bl
                P.apply_delta(full, size, pieces, block)
            else:
                P.recv_file_into(sock, int(resp["size"]), full)
            os.utime(full, (resp["mtime"], resp["mtime"]))
            if resp.get("delta"):
                self.log(f"[{peer_name}] 拉取 {share}/{rel}"
                         f"（增量 {int(resp['dsize'])}/{resp['size']} 字节）")
            else:
                self.log(f"[{peer_name}] 拉取 {share}/{rel}")
        except (OSError, ConnectionError) as e:
            self.log(f"[{peer_name}] 写入 {rel} 失败：{e}")

    def _recv_sig(self, sock, want_share: str, want_rel: str):
        """阻塞等待对端回复指定文件的分块签名。无可用签名时返回 None。"""
        while not self._stop.is_set():
            msg = P.recv_msg(sock)
            if msg.get("type") == "sig" and msg.get("share") == want_share \
                    and msg.get("rel") == want_rel:
                return msg.get("blocks") or None

    def _recv_manifest(self, sock, want_share: str) -> dict:
        """阻塞等待对端回复该共享夹的清单（对端 read_loop 收到 manifest 会立即回复）。
        不使用 socket 级超时：连接被 read_loop 与同步轮共享，settimeout 会污染阻塞中的读循环。
        """
        while not self._stop.is_set():
            msg = P.recv_msg(sock)
            if msg.get("type") == "manifest" and msg.get("share") == want_share:
                return msg.get("files", {})
        return {}

    # ---------------- 定时自动同步（仅主动连接方发起轮次）----------------
    def _auto_loop(self):
        while not self._stop.wait(self.cfg.interval):
            if self.cfg.auto_sync:
                self.run_sync_once()


def hmac_compare(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(str(a), str(b))
