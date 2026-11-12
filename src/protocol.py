# -*- coding: utf-8 -*-
"""线协议：4 字节大端长度 + UTF-8 JSON 消息；文件以二进制流跟随在控制消息之后。

消息类型：
  server -> client: {type:"challenge", nonce}
  client -> server: {type:"hello", name, mac, shares:[...], nonce}
  server -> client: {type:"hello_ack", ok, reason, name, shares:[...], mac}
                    （ack 的 mac = HMAC(token, 服务端 nonce + 客户端 nonce)，用于双向认证）
  双向: {type:"manifest", share, files:{rel:{size,mtime}}}
  双向: {type:"push", share, rel, size, mtime}  随后跟随 size 字节文件内容
        {type:"push", ..., delta:1, block:N, changed:[i...], dsize} 随后跟随 dsize 字节变化块
  双向: {type:"sig_req", share, rel}           对端回 {type:"sig", share, rel, blocks:[md5...]}
  双向: {type:"pull", share, rel, basis:[md5...]}  对端回 {type:"file", size, mtime, [delta:1, block, changed, dsize]}
                                                    + 内容；文件不存在/出错回 {type:"error", reason}
  双向: {type:"delete", share, rel}            对端删除该文件（两端都开启 delete_mirror 时）
  心跳: {type:"ping"}                         主动连接方定期发送的单向保活消息
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

# Windows 保留设备名：写成这些名字会落到设备而不是共享夹里的普通文件
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"}
_WIN_RESERVED |= {f"COM{i}" for i in range(1, 10)}
_WIN_RESERVED |= {f"LPT{i}" for i in range(1, 10)}


class LocalFileChanged(OSError):
    """本地文件在扫描/发送过程中被改写或截断。"""


def safe_rel(rel) -> bool:
    """判断对端/云端给出的相对路径是否可以安全地拼到共享夹根目录下。

    拒绝：空路径、绝对路径、`..`/`.` 段、Windows 驱动器前缀（D:evil）、
    NTFS 数据流（file:stream）、保留设备名（CON/NUL）以及尾随点/空格。
    """
    if not isinstance(rel, str) or not rel or "\x00" in rel:
        return False
    if len(rel) > 2048:
        return False
    if os.path.isabs(rel):
        return False
    normalized = rel.replace("\\", "/")
    if normalized.startswith("/"):
        return False
    for part in normalized.split("/"):
        if part in ("", ".", ".."):
            return False
        if os.name == "nt":
            if ":" in part:
                return False
            trimmed = part.rstrip(" .")
            if not trimmed or trimmed != part:
                return False
            if trimmed.split(".")[0].upper() in _WIN_RESERVED:
                return False
    return True


def safe_join(root, rel):
    """把 rel 安全地拼到 root 下；不安全或越界时返回 None。"""
    if not root or not safe_rel(rel):
        return None
    root_abs = os.path.abspath(root)
    dest = os.path.abspath(os.path.join(root_abs, rel.replace("/", os.sep)))
    try:
        common = os.path.commonpath([root_abs, dest])
    except ValueError:  # 不同盘符
        return None
    if os.path.normcase(common) != os.path.normcase(root_abs):
        return None
    if os.path.normcase(dest) == os.path.normcase(root_abs):
        return None
    return dest


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


def send_stream(sock, f, size: int):
    """把已打开的文件对象按 size 字节发给对端。"""
    remaining = size
    while remaining > 0:
        chunk = f.read(min(CHUNK, remaining))
        if not chunk:
            # 文件在读取过程中被截断（如游戏正在改写存档），必须报错而非静默短发，
            # 否则对端 recv_exact 会永远阻塞。
            raise LocalFileChanged(f"文件在传输前被截断，还差 {remaining} 字节")
        sock.sendall(chunk)
        remaining -= len(chunk)


def send_file(sock, path: str, size: int):
    with open(path, "rb") as f:
        send_stream(sock, f, size)


def recv_file_into(sock, size: int, dest_path: str):
    """先写 .part 临时文件，完成后原子替换，避免读到半截存档。"""
    tmp = dest_path + ".part"
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    try:
        remaining = size
        with open(tmp, "wb") as f:
            while remaining > 0:
                chunk = recv_exact(sock, min(CHUNK, remaining))
                f.write(chunk)
                remaining -= len(chunk)
        os.replace(tmp, dest_path)
    except BaseException:
        # 传输失败时清理半截临时文件，避免在存档目录里越积越多
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


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
    """对比源文件与基础签名，返回 [(块索引, 数据)]，只含有差异的块。

    size 为扫描时记录的文件大小：只读取这么多字节，文件在传输期间变短时抛
    LocalFileChanged，避免把多出来的块发给对端导致流错位。
    """
    changed = []
    idx = 0
    with open(src_path, "rb") as f:
        while True:
            want = min(BLOCK, size - idx * BLOCK)
            if want <= 0:
                break
            chunk = f.read(want)
            if len(chunk) != want:
                raise LocalFileChanged(f"文件在读取过程中变短（块 {idx}）")
            if idx >= len(basis) or hashlib.md5(chunk).hexdigest() != basis[idx]:
                changed.append((idx, chunk))
            idx += 1
    return changed


def parse_delta(msg: dict, data: bytes):
    """校验增量消息并切分出 {块索引: 数据}。

    返回 (size, pieces, blocksize)；字段缺失、块索引越界、长度不一致等
    情况一律抛 ValueError，避免把损坏/恶意的增量写进存档。
    """
    try:
        size = int(msg["size"])
        blocksize = int(msg["block"])
        dsize = int(msg["dsize"])
        changed = msg["changed"]
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("增量消息字段无效") from e
    if size < 0 or blocksize <= 0 or blocksize > MAX_MSG:
        raise ValueError("增量消息参数越界")
    if not isinstance(changed, list):
        raise ValueError("增量块列表无效")
    max_blocks = (size + blocksize - 1) // blocksize
    if len(changed) > max_blocks or dsize != len(data) or dsize > size:
        raise ValueError("增量数据长度不一致")

    pieces = {}
    off = 0
    for raw in changed:
        try:
            idx = int(raw)
        except (TypeError, ValueError) as e:
            raise ValueError("增量块索引无效") from e
        if idx < 0 or idx in pieces:
            raise ValueError("增量块索引越界或重复")
        bl = min(blocksize, size - idx * blocksize)
        if bl <= 0:
            raise ValueError("增量块索引越界")
        piece = data[off:off + bl]
        if len(piece) != bl:
            raise ValueError("增量数据不完整")
        pieces[idx] = piece
        off += bl
    if off != len(data):
        raise ValueError("增量数据长度不匹配")
    return size, pieces, blocksize


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
                if not chunk or len(chunk) > min(blocksize, size - written):
                    raise OSError("增量数据不完整，无法重建文件")
                out.write(chunk)
                written += len(chunk)
                idx += 1
        if basis is not None:
            # Windows 上必须先关掉基础文件，否则 os.replace 会报 WinError 5
            basis.close()
            basis = None
        os.replace(tmp, dest_path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    finally:
        if basis is not None:
            basis.close()
