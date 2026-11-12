# -*- coding: utf-8 -*-
"""局域网自动发现：IPv4 广播 + IPv6 链路本地组播。
异地（跨公网 IPv6）无法自动发现，需在界面手动填写对端 IPv6 地址。

每次 start() 使用独立的停止事件与线程，因此「停止服务 → 启动服务」后
发现功能可以正常恢复（旧实现复用同一个 _stop，重启后线程会立刻退出）。
"""
import json
import socket
import threading
import time

from . import config as cfgmod

ANNOUNCE_INTERVAL = 4.0
PEER_TTL = 15.0  # 超过该秒数未再收到广播则视为离线


class Discovery:
    def __init__(self, cfg: cfgmod.Config, log):
        self.cfg = cfg
        self.log = log
        self.peers = {}  # addr -> {name, port, last_seen, v6:bool}
        self._stop = threading.Event()
        self._threads = []
        self._lock = threading.Lock()

    # ---------- 广播发送 ----------
    def _announce_loop(self, stop: threading.Event):
        payload = json.dumps({
            "type": "announce",
            "name": self.cfg.device_name,
            "port": self.cfg.port,
        }, ensure_ascii=False).encode("utf-8")
        while not stop.is_set():
            # IPv4 广播
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.sendto(payload, ("255.255.255.255", cfgmod.DISCOVERY_PORT))
                s.close()
            except OSError:
                pass
            # IPv6 链路本地组播（仅同一条二层链路有效）
            try:
                s6 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
                s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 1)
                s6.sendto(payload, ("ff02::1", cfgmod.DISCOVERY_PORT, 0, 0))
                s6.close()
            except OSError:
                pass
            stop.wait(ANNOUNCE_INTERVAL)

    # ---------- 监听 ----------
    def _listen_v4(self, stop: threading.Event):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", cfgmod.DISCOVERY_PORT))
            s.settimeout(1.0)
            while not stop.is_set():
                try:
                    data, addr = s.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                self._ingest(data, addr[0], v6=False)
        except OSError as e:
            self.log(f"IPv4 广播监听不可用：{e}")
        finally:
            if s is not None:
                s.close()

    def _listen_v6(self, stop: threading.Event):
        s = None
        try:
            s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("::", cfgmod.DISCOVERY_PORT))
            s.settimeout(1.0)
            while not stop.is_set():
                try:
                    data, addr = s.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                # recvfrom 的 IPv6 地址是 (host, port, flowinfo, scopeid)，
                # 链路本地地址必须带上 scope id 才能连回去
                scope = addr[3] if len(addr) > 3 else 0
                host = addr[0] if not scope or "%" in addr[0] else f"{addr[0]}%{scope}"
                self._ingest(data, host, v6=True)
        except OSError:
            self.log("IPv6 组播监听不可用，仅使用 IPv4 广播发现")
        finally:
            if s is not None:
                s.close()

    def _ingest(self, data: bytes, addr: str, v6: bool):
        try:
            msg = json.loads(data.decode("utf-8"))
        except Exception:
            return
        if msg.get("type") != "announce":
            return
        if msg.get("name") == self.cfg.device_name:
            return  # 忽略自己
        with self._lock:
            self.peers[addr] = {
                "name": msg.get("name", "?"),
                "port": int(msg.get("port", self.cfg.port)),
                "last_seen": time.time(),
                "v6": v6,
            }

    def snapshot(self) -> list:
        now = time.time()
        out = []
        with self._lock:
            for addr, p in list(self.peers.items()):
                if now - p["last_seen"] > PEER_TTL:
                    self.peers.pop(addr, None)
                    continue
                out.append({"addr": addr, **p})
        out.sort(key=lambda x: x["name"])
        return out

    def start(self):
        # 每次启动用新的停止事件，旧线程即使还在退出也不会影响新线程
        stop = threading.Event()
        self._stop = stop
        self._threads = [
            threading.Thread(target=self._announce_loop, args=(stop,), daemon=True),
            threading.Thread(target=self._listen_v4, args=(stop,), daemon=True),
            threading.Thread(target=self._listen_v6, args=(stop,), daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self):
        self._stop.set()
