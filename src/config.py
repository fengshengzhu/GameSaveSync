# -*- coding: utf-8 -*-
"""配置管理。

数据目录规则：
  - 打包运行（PyInstaller）：统一放 %APPDATA%\\GameSaveSync。exe 可能被用户放在
    桌面、下载等任意位置，不应在这些地方生成文件；且 PyInstaller 单文件模式的
    __file__ 位于每次启动都不同的临时解压目录（%TEMP%\\_MEIxxxxxx，退出即删除），
    绝不能用来定位配置。
  - 源码运行：用项目目录，方便开发调试。
启动时会调用 migrate_legacy_files() 把旧版本放在 exe 旁边的配置迁移到新位置。
"""
import json
import os
import re
import shutil
import socket
import secrets
import platform
import sys


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        app_dir = os.path.join(base, "GameSaveSync")
    else:
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(app_dir, exist_ok=True)
    return app_dir


APP_DIR = _app_dir()
CONFIG_PATH = os.path.join(APP_DIR, "sync_config.json")

VERSION = "1.3.0"

# 旧版本（v1.1.1 及之前）可能写在 exe 旁边的文件
LEGACY_FILES = ("sync_config.json", "webdav_state.json")


def migrate_legacy_files() -> list:
    """把旧版本遗留在 exe 同目录的配置/状态文件迁移到 APP_DIR。

    只在目标位置没有同名文件时移动（不覆盖新数据）；返回日志行列表。
    """
    if not getattr(sys, "frozen", False):
        return []
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if os.path.normcase(exe_dir) == os.path.normcase(APP_DIR):
        return []
    moved = []
    for name in LEGACY_FILES:
        src = os.path.join(exe_dir, name)
        dst = os.path.join(APP_DIR, name)
        if os.path.isfile(src) and not os.path.exists(dst):
            try:
                # exe 可能与 APPDATA 在不同分区，os.replace 无法跨盘移动，用 shutil.move
                shutil.move(src, dst)
                moved.append(f"已迁移 {name} -> {dst}")
            except OSError:
                pass
    return moved

DEFAULT_PORT = 29876
DISCOVERY_PORT = 39876


def default_device_name() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "PC"


def generate_token(n: int = 12) -> str:
    """生成易输入的配对码（去除易混淆字符）。"""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


class Config:
    def __init__(self, data: dict):
        self.raw = data

    # ---- 基础 ----
    @property
    def device_name(self) -> str:
        return str(self.raw.get("device_name") or default_device_name())

    @device_name.setter
    def device_name(self, v: str):
        self.raw["device_name"] = v.strip() or default_device_name()

    @property
    def token(self) -> str:
        return str(self.raw.get("token") or "")

    @token.setter
    def token(self, v: str):
        self.raw["token"] = v.strip()

    @property
    def port(self) -> int:
        try:
            p = int(self.raw.get("port", DEFAULT_PORT))
            return p if 1024 <= p <= 65535 else DEFAULT_PORT
        except (TypeError, ValueError):
            return DEFAULT_PORT

    @port.setter
    def port(self, v: int):
        p = int(v)
        if not (1024 <= p <= 65535):
            raise ValueError("端口必须在 1024-65535 之间")
        self.raw["port"] = p

    # ---- 共享文件夹 ----
    @property
    def shares(self) -> list:
        """[{name, path}]，按名称配对。"""
        out = []
        for item in self.raw.get("shares", []):
            if isinstance(item, dict) and item.get("name") and item.get("path"):
                out.append({"name": str(item["name"]), "path": str(item["path"])})
        return out

    def add_share(self, name: str, path: str):
        name, path = name.strip(), os.path.abspath(path)
        self.remove_share(name)
        self.raw.setdefault("shares", []).append({"name": name, "path": path})

    def remove_share(self, name: str):
        self.raw["shares"] = [s for s in self.raw.get("shares", []) if s.get("name") != name]

    def share_path(self, name: str):
        for s in self.shares:
            if s["name"] == name:
                return s["path"]
        return None

    # ---- 选项 ----
    @property
    def auto_sync(self) -> bool:
        return bool(self.raw.get("auto_sync", True))

    @auto_sync.setter
    def auto_sync(self, v: bool):
        self.raw["auto_sync"] = bool(v)

    @property
    def interval(self) -> int:
        try:
            v = int(self.raw.get("interval", 10))
            return max(3, v)
        except (TypeError, ValueError):
            return 10

    @interval.setter
    def interval(self, v: int):
        self.raw["interval"] = max(3, int(v))

    @property
    def delete_mirror(self) -> bool:
        """默认关闭：只新增/覆盖，不删除，避免误删存档。"""
        return bool(self.raw.get("delete_mirror", False))

    @delete_mirror.setter
    def delete_mirror(self, v: bool):
        self.raw["delete_mirror"] = bool(v)

    # ---- 手动对端地址（IPv4 或 IPv6，可带端口）----
    @property
    def manual_peers(self) -> list:
        return [str(x) for x in self.raw.get("manual_peers", []) if str(x).strip()]

    # ---- WebDAV 云盘备份（Cloudreve 等）----
    @property
    def webdav_enabled(self) -> bool:
        return bool(self.raw.get("webdav_enabled", False))

    @webdav_enabled.setter
    def webdav_enabled(self, v: bool):
        self.raw["webdav_enabled"] = bool(v)

    @property
    def webdav_url(self) -> str:
        return str(self.raw.get("webdav_url") or "").strip()

    @webdav_url.setter
    def webdav_url(self, v: str):
        self.raw["webdav_url"] = str(v).strip()

    @property
    def webdav_user(self) -> str:
        return str(self.raw.get("webdav_user") or "")

    @webdav_user.setter
    def webdav_user(self, v: str):
        self.raw["webdav_user"] = str(v).strip()

    @property
    def webdav_pass(self) -> str:
        return str(self.raw.get("webdav_pass") or "")

    @webdav_pass.setter
    def webdav_pass(self, v: str):
        self.raw["webdav_pass"] = str(v)

    @property
    def webdav_insecure(self) -> bool:
        """跳过 TLS 证书校验：仅用于自签名证书或证书链异常（如系统证书库里的
        过期交叉证书导致 verify failed）的自建服务器。流量仍然加密。"""
        return bool(self.raw.get("webdav_insecure", False))

    @webdav_insecure.setter
    def webdav_insecure(self, v: bool):
        self.raw["webdav_insecure"] = bool(v)

    @property
    def webdav_dir(self) -> str:
        """远端根目录（相对 WebDAV 地址），默认 GameSaveSync。"""
        d = str(self.raw.get("webdav_dir") or "").strip().strip("/")
        return d or "GameSaveSync"

    @webdav_dir.setter
    def webdav_dir(self, v: str):
        self.raw["webdav_dir"] = str(v).strip().strip("/")

    @property
    def webdav_shares(self) -> list:
        """要备份的共享夹名称列表；为空表示备份全部。"""
        return [str(x) for x in self.raw.get("webdav_shares", []) if str(x).strip()]

    # ---- 窗口尺寸记忆 ----
    @property
    def window_geometry(self) -> str:
        """形如 '780x620' 的窗口尺寸；无效或缺失时返回空串。"""
        g = str(self.raw.get("window_geometry") or "")
        return g if re.fullmatch(r"\d{3,5}x\d{3,5}", g) else ""

    @window_geometry.setter
    def window_geometry(self, v: str):
        v = str(v or "").strip()
        if re.fullmatch(r"\d{3,5}x\d{3,5}", v):
            self.raw["window_geometry"] = v

    def save(self):
        # 落盘前把越界的历史值归一化，避免界面显示与运行端口不一致
        self.raw["port"] = self.port
        self.raw["interval"] = self.interval
        self.raw["platform"] = platform.system()
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.raw, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)


def load() -> Config:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return Config(data)
        except Exception:
            pass
    data = {
        "device_name": default_device_name(),
        "token": generate_token(),
        "port": DEFAULT_PORT,
        "shares": [],
        "auto_sync": True,
        "interval": 10,
        "delete_mirror": False,
        "manual_peers": [],
        "webdav_enabled": False,
        "webdav_url": "",
        "webdav_user": "",
        "webdav_pass": "",
        "webdav_insecure": False,
        "webdav_dir": "GameSaveSync",
        "webdav_shares": [],
    }
    cfg = Config(data)
    cfg.save()
    return cfg


def local_addresses() -> list:
    """返回本机可用的 IPv4 / 全局 IPv6 地址，供用户异地手动填写。"""
    result = []
    # IPv4：通过“假装连接”拿到出口网卡地址，不真正发包
    for fam, typ, _, _, sa in socket.getaddrinfo(socket.gethostname(), None):
        ip = sa[0]
        if fam == socket.AF_INET and not ip.startswith("127.") and ip not in result:
            result.append(ip)
    # IPv6：遍历 hostname 解析结果，过滤回环/链路本地/私有 ULA 之外的全局地址
    try:
        for fam, _, _, _, sa in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6):
            ip = sa[0]
            if ip in ("::1",) or ip.lower().startswith("fe80") or ip.lower().startswith("fc") \
                    or ip.lower().startswith("fd") or "%" in ip:
                continue
            if ip not in result:
                result.append(ip)
    except Exception:
        pass
    # 兜底：UDP 外推法
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and ip not in result:
            result.insert(0, ip)
    except Exception:
        pass
    return result
