# -*- coding: utf-8 -*-
"""线协议：4 字节大端长度 + UTF-8 JSON 消息；文件以二进制流跟随在控制消息之后。

消息类型：
  server -> client: {type:"challenge", nonce}
  client -> server: {type:"hello", name, mac, shares:[...]}
  server -> client: {type:"hello_ack", ok, reason, name, shares:[...]}
  双向: {type:"manifest", share, files:{rel:{size,mtime}}}
  双向: {type:"push", share, rel, size, mtime}  随后跟随 size 字节文件内容
        {type:"push", ..., delta:1, block:N, changed:[i...], dsize} 随后跟随 dsize 字节变化块
  双向: {type:"sig_req", share, rel}           对端回 {type:"sig", share, rel, blocks:[md5...]}
  双向: {type:"pull", share, rel, basis:[md5...]}  对端回 {type:"file", size, mtime, [delta:1, block, changed, dsize]}
                                                    + 内容；文件不存在/出错回 {type:"error", reason}
  双向: {type:"delete", share, rel}            对端删除该文件（delete_mirror 开启时）
  心跳: {type:"ping"} / {type:"pong"}
"""
import hashlib
import hmac
import json
import os
import struct

HEADER = struct.Struct(">I")
CHUNK = 64 * 1024
MARGIN = 2.0  # 时钟偏差容忍（秒），避免两端时间差导致反复互推
MAX_MSG = 16 * 1024 * 1024  # 控制消息（含签名清单）大小上限，防异常长度帧
BLOCK = 256 * 1024          # 增量传输分块大小
DELTA_THRESHOLD = 1024 * 1024  # 超过该字节数才尝试增量（小文件全量更快）


def mac(token: str, nonce: str) -> str:
    return hmac.new(token.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def send_msg(sock, obj: dict):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sock.sendall(HEADER.pack(len(data)) + data)


def recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接已断开")
        buf.extend(chunk)
    return bytes(buf)


def recv_msg(sock) -> dict:
    raw = recv_exact(sock, HEADER.size)
    (n,) = HEADER.unpack(raw)
    if n > MAX_MSG:
        raise ValueError(f"控制消息过大（{n} 字节），拒绝接收")
    data = recv_exact(sock, n)
    return json.loads(data.decode("utf-8"))


def send_file(sock, path: str, size: int):
    with open(path, "rb") as f:
        remaining = size
        while remaining > 0:
            chunk = f.read(min(CHUNK, remaining))
            if not chunk:
                # 文件在读取过程中被截断（如游戏正在改写存档），必须报错而非静默短发，
                # 否则对端 recv_exact 会永远阻塞。
                raise OSError(f"文件在传输前被截断，还差 {remaining} 字节")
            sock.sendall(chunk)
            remaining -= len(chunk)


def recv_file_into(sock, size: int, dest_path: str):
    """先写 .part 临时文件，完成后原子替换，避免读到半截存档。"""
    tmp = dest_path + ".part"
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    remaining = size
    with open(tmp, "wb") as f:
        while remaining > 0:
            chunk = recv_exact(sock, min(CHUNK, remaining))
            f.write(chunk)
            remaining -= len(chunk)
    os.replace(tmp, dest_path)


def list_share(root: str) -> dict:
    """扫描目录，返回 {相对路径: {size, mtime}}。mtime 用 UTC epoch 秒。"""
    files = {}
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return files
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if name.endswith(".part"):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace("\\", "/")
            files[rel] = {"size": st.st_size, "mtime": st.st_mtime}
    return files


def decide(local: dict, remote: dict) -> dict:
    """根据双方清单决定动作。返回 {push:[rel...], pull:[rel...]}。
    规则：仅本地有 -> push；仅远端有 -> pull；都有 -> mtime 新的一方覆盖旧方。
    """
    push, pull = [], []
    for rel, meta in local.items():
        if rel not in remote:
            push.append(rel)
        else:
            r = remote[rel]
            if meta["mtime"] > r["mtime"] + MARGIN:
                push.append(rel)
            elif r["mtime"] > meta["mtime"] + MARGIN:
                pull.append(rel)
            elif meta["size"] != r["size"]:
                # mtime 接近但大小不同：以本地为准推一次（保守，避免漏传）
                push.append(rel)
    for rel in remote:
        if rel not in local:
            pull.append(rel)
    return {"push": push, "pull": pull}


# ---------------- 增量传输（分块 MD5 delta） ----------------

def block_hashes(path: str) -> list:
    """按固定分块计算文件的 MD5 签名列表。"""
    hashes = []
    with open(path, "rb") as f:
        while True:
            chunk = f.read(BLOCK)
            if not chunk:
                break
            hashes.append(hashlib.md5(chunk).hexdigest())
    return hashes


def changed_blocks(src_path: str, size: int, basis: list) -> list:
    """对比源文件与基础签名，返回 [(块索引, 数据)]，只含有差异的块。"""
    changed = []
    idx = 0
    with open(src_path, "rb") as f:
        while True:
            chunk = f.read(BLOCK)
            if not chunk:
                break
            if idx >= len(basis) or hashlib.md5(chunk).hexdigest() != basis[idx]:
                changed.append((idx, chunk))
            idx += 1
    return changed


def apply_delta(dest_path: str, size: int, blocks: dict, blocksize: int):
    """用变化块 + 对端本地旧文件（作为基础）重建新文件，先写 .part 再原子替换。
    blocks: {块索引: 数据}。基础文件不存在时所有块必须齐全。
    """
    tmp = dest_path + ".part"
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    basis = open(dest_path, "rb") if os.path.isfile(dest_path) else None
    try:
        with open(tmp, "wb") as out:
            written = 0
            idx = 0
            while written < size:
                if idx in blocks:
                    chunk = blocks[idx]
                elif basis is not None:
                    # 按块索引显式定位，跳过由增量块替代的基础块，避免错位
                    bl = min(blocksize, size - written)
                    basis.seek(idx * blocksize)
                    chunk = basis.read(bl)
                else:
                    raise OSError("增量数据不完整，无法重建文件")
                if not chunk:
                    raise OSError("增量数据不完整，无法重建文件")
                out.write(chunk)
                written += len(chunk)
                idx += 1
    finally:
        if basis is not None:
            basis.close()
    os.replace(tmp, dest_path)
