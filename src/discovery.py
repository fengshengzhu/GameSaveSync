# -*- coding: utf-8 -*-
"""局域网自动发现：IPv4 广播 + IPv6 链路本地组播。
异地（跨公网 IPv6）无法自动发现，需在界面手动填写对端 IPv6 地址。
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
        self._sock_v4 = None
        self._sock_v6 = None
        self._lock = threading.Lock()

    # ---------- 广播发送 ----------
    def _announce_loop(self):
        payload = json.dumps({
            "type": "announce",
            "name": self.cfg.device_name,
            "port": self.cfg.port,
        }, ensure_ascii=False).encode("utf-8")
        while not self._stop.is_set():
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
            self._stop.wait(ANNOUNCE_INTERVAL)

    # ---------- 监听 ----------
    def _listen_v4(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", cfgmod.DISCOVERY_PORT))
        s.settimeout(1.0)
        self._sock_v4 = s
        while not self._stop.is_set():
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            self._ingest(data, addr[0], v6=False)
        s.close()

    def _listen_v6(self):
        try:
            s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("::", cfgmod.DISCOVERY_PORT))
            s.settimeout(1.0)
            self._sock_v6 = s
            while not self._stop.is_set():
                try:
                    data, addr = s.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                self._ingest(data, addr[0], v6=True)
            s.close()
        except OSError:
            self.log("IPv6 组播监听不可用，仅使用 IPv4 广播发现")

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
        threading.Thread(target=self._announce_loop, daemon=True).start()
        threading.Thread(target=self._listen_v4, daemon=True).start()
        threading.Thread(target=self._listen_v6, daemon=True).start()

    def stop(self):
        self._stop.set()
        for s in (self._sock_v4, self._sock_v6):
            try:
                s and s.close()
            except OSError:
                pass
