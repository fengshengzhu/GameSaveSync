# -*- coding: utf-8 -*-
"""同步引擎：双栈(TCP v4/v6)监听、配对握手、清单交换、双向 LWW 传输、定时自动同步。"""
import os
import socket
import threading

from . import config as cfgmod
from . import protocol as P
from .discovery import Discovery
from .webdav import BackupWorker

# 单次阻塞读写的上限（秒）。存档通常很快传完；超时说明对端失联或链路已断。
IO_TIMEOUT = 60.0
# 主动连接方发送心跳的间隔；被动方超过 HEARTBEAT_TIMEOUT 收不到任何数据即断开。
# 超时留得比心跳间隔宽裕，避免主动方在扫描大目录/计算大文件签名时被误判。
HEARTBEAT_INTERVAL = 15.0
HEARTBEAT_TIMEOUT = 120.0


def parse_target(text: str):
    """解析对端地址为 (host, port, family_hint)。
    支持: 192.168.1.5 / 192.168.1.5:29876 / [240e:xxxx::1] / [240e:xxxx::1]:29876 / 裸IPv6。
    """
    text = text.strip()
    port = None
    host = text
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ValueError("IPv6 地址缺少右方括号")
        host = text[1:end]
        rest = text[end + 1:]
        if rest.startswith(":"):
            port = int(rest[1:])
    elif ":" in text and text.count(":") == 1:
        host, p = text.rsplit(":", 1)
        port = int(p)
    if not host:
        raise ValueError("地址为空")
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("端口必须在 1-65535 之间")
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
        self.webdav = BackupWorker(cfg, log)  # WebDAV 云盘备份，与 P2P 同步独立
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
        self.webdav.start()
        self.log(f"服务已启动，监听端口 {self.cfg.port}（同时接受 IPv4 与 IPv6）")

    def stop(self):
        self._stop.set()
        self.discovery.stop()
        self.webdav.stop()
        # 停止期间发生的删除不应在下次启动时被当成“本机删除”传播
        self._last_scan.clear()
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
            if not self.cfg.token:
                P.send_msg(conn, {"type": "hello_ack", "ok": False,
                                  "reason": "本机未设置配对码"})
                self.log("拒绝连接：本机配对码为空，请先在「本机设置」里填写或生成")
                return
            expect = P.mac(self.cfg.token, nonce)
            if not hmac_compare(hello.get("mac", ""), expect):
                P.send_msg(conn, {"type": "hello_ack", "ok": False, "reason": "配对码不匹配"})
                self.log(f"拒绝来自 {addr[0]} 的连接：配对码错误")
                return
            peer_name = hello.get("name", addr[0])
            client_nonce = hello.get("nonce", "")
            if not isinstance(client_nonce, str) or len(client_nonce) > 256:
                client_nonce = ""
            local_shares = {s["name"] for s in self.cfg.shares}
            # 反向认证：让客户端确认对端也持有配对码，而不是任何能接受连接的冒充者
            P.send_msg(conn, {"type": "hello_ack", "ok": True, "name": self.cfg.device_name,
                              "shares": sorted(local_shares),
                              "mac": P.mac(self.cfg.token, nonce + client_nonce)})
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
        if not self.cfg.token:
            self.log("配对码为空：请先在「本机设置」里填写或点「重新生成」")
            return False
        try:
            host, port, family = parse_target(target)
        except ValueError as e:
            self.log(f"地址格式错误：{target}（{e}）")
            return False
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(10)
        try:
            sock.connect((host, port))
        except (OSError, ValueError) as e:
            self.log(f"连接 {target} 失败：{e}")
            sock.close()
            return False
        try:
            ch = P.recv_msg(sock)
            if ch.get("type") != "challenge":
                raise ConnectionError("对端未返回握手挑战")
            client_nonce = os.urandom(16).hex()
            local_shares = {s["name"] for s in self.cfg.shares}
            P.send_msg(sock, {
                "type": "hello",
                "name": self.cfg.device_name,
                "mac": P.mac(self.cfg.token, ch["nonce"]),
                "shares": sorted(local_shares),
                "nonce": client_nonce,
            })
            ack = P.recv_msg(sock)
            if not ack.get("ok"):
                self.log(f"被 {host} 拒绝：{ack.get('reason', '未知原因')}")
                sock.close()
                return False
            # 双向认证：服务端必须证明自己也持有配对码，防止有人冒充对端接收存档
            expect = P.mac(self.cfg.token, str(ch.get("nonce", "")) + client_nonce)
            if not hmac_compare(ack.get("mac", ""), expect):
                self.log(f"对端身份校验失败：{host}（配对码不一致，或对端版本过旧，"
                         "两端需同时升级到 v1.3.0+）")
                sock.close()
                return False
            peer_name = ack.get("name", host)
            info = self._register(sock, peer_name, (host, port), set(ack.get("shares", [])), initiator=True)
            sock.settimeout(IO_TIMEOUT)
            self.log(f"已连接：{peer_name}（{host}），共享夹：{', '.join(ack.get('shares', [])) or '无'}")
            threading.Thread(target=self._heartbeat_loop, args=(peer_name, info),
                             daemon=True, name=f"GameSaveSync-HB-{peer_name}").start()
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

    @staticmethod
    def _abort_connection(sock):
        """消息流已经错位时直接断开，别让对端永远等一个不会来的响应。"""
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _heartbeat_loop(self, peer_name: str, info: dict):
        """主动连接方定期发 ping，让被动方的读循环能区分空闲和失联。"""
        while not self._stop.wait(HEARTBEAT_INTERVAL):
            if self._conns.get(peer_name) is not info:
                return
            with info["lock"]:
                if self._conns.get(peer_name) is not info:
                    return
                try:
                    P.send_msg(info["sock"], {"type": "ping"})
                except (OSError, ValueError):
                    self.log(f"[{peer_name}] 心跳发送失败，连接已断开")
                    self._teardown_initiator(peer_name, info)
                    return

    # ---------------- 读取循环：响应对端的 push/pull/manifest ----------------
    def _read_loop(self, sock: socket.socket, peer_name: str, info: dict):
        # 主动方每 15 秒发一次心跳；超过 HEARTBEAT_TIMEOUT 收不到任何数据即视为失联
        sock.settimeout(HEARTBEAT_TIMEOUT)
        lock = info["lock"]
        while not self._stop.is_set():
            try:
                msg = P.recv_msg(sock)
            except socket.timeout:
                self.log(f"[{peer_name}] 心跳超时，断开连接")
                break
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
                        self._reply_manifest(sock, msg, peer_name)
                    elif mtype == "sig_req":
                        self._handle_sig_req(sock, msg)
                    elif mtype == "delete":
                        self._handle_delete(msg, peer_name)
                    elif mtype == "ping":
                        pass  # 单向保活，不需要回复（回复会塞满对方从不读取的接收缓冲）
                except (ConnectionError, OSError, ValueError) as e:
                    self.log(f"[{peer_name}] 处理消息出错：{type(e).__name__}: {e}")
                    break
        self._unregister_by_sock(sock)

    def _reply_manifest(self, sock, msg, peer_name: str = None):
        share = msg.get("share")
        root = self._share_root(share)
        files = P.list_share(root) if root and os.path.isdir(root) else {}
        # 被动方也检测本机删除：对比上一轮的扫描结果，把删除事件告诉主动方。
        # 只有两端都开启 delete_mirror 时才会真正互删。
        if self.cfg.delete_mirror:
            key = (peer_name, share)
            last = self._last_scan.get(key)
            if last is not None:
                for rel in last:
                    if rel not in files:
                        P.send_msg(sock, {"type": "delete", "share": share, "rel": rel})
                        self.log(f"[{peer_name}] 传播删除 {share}/{rel}")
            self._last_scan[key] = files
        P.send_msg(sock, {"type": "manifest", "share": share, "files": files})

    def _share_root(self, share: str):
        return self.cfg.share_path(share)

    def _handle_push(self, sock, msg, peer_name):
        share, rel = msg.get("share"), msg.get("rel")
        root = self._share_root(share)
        dest = P.safe_join(root, rel)
        if not dest:
            # 无法安全落盘，且正文还没读走，只能断开以免消息流错位
            self.log(f"[{peer_name}] 拒绝非法推送路径：{share}/{rel}")
            self._abort_connection(sock)
            return
        try:
            if msg.get("delta"):
                size = int(msg["size"])
                dsize = int(msg["dsize"])
                if size < 0 or dsize < 0 or dsize > size:
                    raise ValueError("增量长度异常")
                data = P.recv_exact(sock, dsize)
                size, pieces, block = P.parse_delta(msg, data)
                P.apply_delta(dest, size, pieces, block)
            else:
                P.recv_file_into(sock, int(msg["size"]), dest)
            os.utime(dest, (msg["mtime"], msg["mtime"]))
            self.log(f"[{peer_name}] 收到 {share}/{rel}")
        except (OSError, ConnectionError, ValueError, KeyError, TypeError) as e:
            self.log(f"[{peer_name}] 接收 {share}/{rel} 失败：{e}")
            # 正文可能只收了一半，继续读下一条消息必然错位
            self._abort_connection(sock)

    def _handle_pull(self, sock, msg, peer_name):
        share, rel = msg.get("share"), msg.get("rel")
        root = self._share_root(share)
        full = P.safe_join(root, rel)
        if not full or not os.path.isfile(full):
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
        except (OSError, ConnectionError, ValueError) as e:
            # 已经发出 file 头甚至部分正文，对端不会再收到完整的文件；必须断开，
            # 否则它会一直阻塞等待剩余字节（这就是旧版 pull 卡死的根因）。
            self.log(f"[{peer_name}] 发送 {share}/{rel} 失败：{e}，断开连接")
            self._abort_connection(sock)

    def _handle_sig_req(self, sock, msg):
        """对端索取某文件（它本地的旧版本）的分块签名，用于计算增量。"""
        share, rel = msg.get("share"), msg.get("rel")
        root = self._share_root(share)
        full = P.safe_join(root, rel)
        blocks = []
        if full and os.path.isfile(full):
            try:
                blocks = P.block_hashes(full)
            except OSError:
                blocks = []
        P.send_msg(sock, {"type": "sig", "share": share, "rel": rel, "blocks": blocks})

    def _handle_delete(self, msg, peer_name):
        share, rel = msg.get("share"), msg.get("rel")
        if not self.cfg.delete_mirror:
            self.log(f"[{peer_name}] 忽略删除请求（本机未开启删除镜像）：{share}/{rel}")
            return
        root = self._share_root(share)
        full = P.safe_join(root, rel)
        if not full:
            return
        try:
            if os.path.isfile(full):
                os.remove(full)
                self.log(f"[{peer_name}] 删除镜像 {share}/{rel}")
                # 顺手清理变空的父目录（不越过共享夹根）
                parent = os.path.dirname(full)
                root_abs = os.path.abspath(root)
                while os.path.abspath(parent) != root_abs and \
                        os.path.normcase(os.path.commonpath([root_abs, os.path.abspath(parent)])) \
                        == os.path.normcase(root_abs):
                    try:
                        os.rmdir(parent)
                    except OSError:
                        break
                    parent = os.path.dirname(parent)
        except (OSError, ValueError) as e:
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
            remote_manifest = self._recv_manifest(sock, share, peer_name)
            plan = P.decide(local, remote_manifest)
            for rel in plan["push"]:
                full = P.safe_join(root, rel)
                if not full:
                    self.log(f"[{peer_name}] 跳过非法路径：{share}/{rel}")
                    continue
                # 先打开文件：游戏占用/权限不足等本地问题只跳过这个文件，
                # 不再像旧版那样把整条连接拆掉。
                try:
                    f = open(full, "rb")
                    st = os.fstat(f.fileno())
                except OSError as e:
                    self.log(f"[{peer_name}] 跳过 {share}/{rel}：{e}")
                    continue
                header_sent = False
                try:
                    if self._push_delta(sock, peer_name, share, rel, full, st,
                                        rel in remote_manifest):
                        continue
                    st2 = os.fstat(f.fileno())
                    if st2.st_size != st.st_size:
                        self.log(f"[{peer_name}] 跳过 {share}/{rel}：文件仍在变化")
                        continue
                    P.send_msg(sock, {"type": "push", "share": share, "rel": rel,
                                      "size": st2.st_size, "mtime": st2.st_mtime})
                    header_sent = True
                    P.send_stream(sock, f, st2.st_size)
                    self.log(f"[{peer_name}] 推送 {share}/{rel}")
                except P.LocalFileChanged as e:
                    if header_sent:
                        # 已经发出头部，对端正等着剩余字节，只能断开重来
                        raise ConnectionError(f"{rel} 传输过程中被改写：{e}") from e
                    self.log(f"[{peer_name}] 跳过 {share}/{rel}：{e}")
                finally:
                    f.close()
            for rel in plan["pull"]:
                full = P.safe_join(root, rel)
                if not full:
                    self.log(f"[{peer_name}] 跳过非法路径：{share}/{rel}")
                    continue
                self._pull_file(sock, peer_name, share, rel, full)

    def _push_delta(self, sock, peer_name, share, rel, full, st, remote_has: bool) -> bool:
        """大文件推送前先取对端旧文件的分块签名，只传差异块。返回是否已按增量发送。"""
        if not remote_has or st.st_size <= P.DELTA_THRESHOLD:
            return False
        try:
            P.send_msg(sock, {"type": "sig_req", "share": share, "rel": rel})
            blocks = self._recv_sig(sock, share, rel, peer_name)
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
            size = int(resp["size"])
            if size < 0:
                raise ValueError("文件长度无效")
            if resp.get("delta"):
                dsize = int(resp["dsize"])
                if dsize < 0 or dsize > size:
                    raise ValueError("增量长度无效")
                data = P.recv_exact(sock, dsize)
                size, pieces, block = P.parse_delta(resp, data)
                P.apply_delta(full, size, pieces, block)
            else:
                P.recv_file_into(sock, size, full)
            os.utime(full, (resp["mtime"], resp["mtime"]))
            if resp.get("delta"):
                self.log(f"[{peer_name}] 拉取 {share}/{rel}"
                         f"（增量 {int(resp['dsize'])}/{resp['size']} 字节）")
            else:
                self.log(f"[{peer_name}] 拉取 {share}/{rel}")
        except (OSError, ConnectionError, ValueError, KeyError, TypeError) as e:
            # 正文可能只收到一半，继续下一轮会把流读错位，必须断开重连
            self.log(f"[{peer_name}] 写入 {share}/{rel} 失败：{e}，断开连接")
            raise ConnectionError(f"拉取 {share}/{rel} 失败：{e}") from e

    def _consume_async(self, msg, peer_name: str):
        """同步轮等待回复时可能穿插收到的非应答消息。"""
        mtype = msg.get("type")
        if mtype == "delete":
            self._handle_delete(msg, peer_name)
            # 刚被删除的文件不应在下一轮再被当成“本机删除”发回去
            last = self._last_scan.get((peer_name, msg.get("share")))
            if last is not None:
                last.pop(msg.get("rel"), None)
        # ping 由主动方发送，这里通常收不到；忽略即可

    def _recv_sig(self, sock, want_share: str, want_rel: str, peer_name: str):
        """阻塞等待对端回复指定文件的分块签名。无可用签名时返回 None。"""
        while not self._stop.is_set():
            msg = P.recv_msg(sock)
            if msg.get("type") == "sig" and msg.get("share") == want_share \
                    and msg.get("rel") == want_rel:
                return msg.get("blocks") or None
            self._consume_async(msg, peer_name)

    def _recv_manifest(self, sock, want_share: str, peer_name: str) -> dict:
        """等待对端回复该共享夹的清单；期间可能先收到被动方发来的删除通知。

        主动方 socket 已设置 IO_TIMEOUT，失联时会抛 socket.timeout（OSError），
        由上层断开连接，不会再永久卡住。
        """
        while not self._stop.is_set():
            msg = P.recv_msg(sock)
            if msg.get("type") == "manifest" and msg.get("share") == want_share:
                return msg.get("files", {})
            self._consume_async(msg, peer_name)
        return {}

    # ---------------- 定时自动同步（仅主动连接方发起轮次）----------------
    def _auto_loop(self):
        while not self._stop.wait(self.cfg.interval):
            if self.cfg.auto_sync:
                self.run_sync_once()


def hmac_compare(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(str(a), str(b))
