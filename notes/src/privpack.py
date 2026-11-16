# -*- coding: utf-8 -*-
"""私密文件夹的加密包（privpack v1）—— 格式定义与读写。

格式说明见 `D:\\RAG\\枢阁-私密文件夹-加密包格式.md`。要点：

    包密钥 K_pack（每包随机一把）
       ├─ 用库公钥封存写进包头（x25519:…）  → 锁着也能往包里写（不需要密码）
       └─ 加密包体                          → 读要主密钥 → 私钥 → 开盒 → K_pack

盘上：`个人\\私密\\.privpack` 一个文件。**文件名在加密的包体里**，包头只放计数与时间。

两处刻意的设计（都是被实测逼出来的，不是凭空的偏好）：

  · **包体是二进制，不是 JSON+base64**。第一版把明文 base64 塞进 JSON 再整体加密
    → **base64 编了两遍**，实测膨胀 37%（45410 → 62196 字节）。私密文件夹是能拖大文件的，
    这个浪费随体积线性放大。改成"长度前缀 + 原始字节"后膨胀约 0.1%。
  · **帧哈希进包头**。`key_ref` 是帧字段，在 GCM 认证的信封**之外** —— 实测翻它一位
    parse 不会拒绝（虽然最终解密一定失败，但报错点错、说不清是帧坏了）。
    现在把 header 明文与 key_ref 一起做 SHA-256 写进包头，parse 时就能定位帧损坏。

加密原语一律复用 `vaultcrypto`（不另造一套）。
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import time

import vaultcrypto

MAGIC = b"SHUGEVPK"
VERSION = 1
DEFAULT_NAME = ".privpack"

# 条目头：folder_len, name_len, mtime_len, size, data_len
_HDR_FMT = ">HHHQQ"
_HDR_SIZE = struct.calcsize(_HDR_FMT)


class PackError(Exception):
    """包坏了 / 不是包 / 解不开"""


def sha_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ── 密钥 ─────────────────────────────────────────────────────

def seal_key(k_pack: bytes, vault_pub_b64: str) -> str:
    """把包密钥用库公钥封存 → key_ref（**不需要主密钥**，锁着也能做）"""
    if not vault_pub_b64:
        raise PackError("库里没有公钥（`vault_pub` 为空）：先设一次库密码")
    return vaultcrypto.wrap_box(k_pack, vault_pub_b64)


def open_key(key_ref: str, master: bytes, vault_priv_raw: bytes | None) -> bytes:
    """由 key_ref 取回包密钥。要主密钥（私钥本身是用主密钥包着的）。"""
    if not master:
        raise PackError("锁定态：包解不开（要主密钥）")
    return vaultcrypto.open_key_ref(master, key_ref, vault_priv_raw)


# ── 包体：二进制条目流 ───────────────────────────────────────
#
#   body 明文布局（整体再被 GCM 加密）：
#     uint32  n
#     每条目：
#       folder_len(2) name_len(2) mtime_len(2) size(8) data_len(8)
#       folder 字节 | name 字节 | mtime 字节 | data 字节
#   size 与 data_len 现在是同一个数：data_len 是**实际存的**字节数，留着是为了格式能演进
#   （将来做分块/压缩时，size 记原始长度、data_len 记存储长度）。

def _build_body(items: list) -> bytes:
    out = bytearray()
    out += struct.pack(">I", len(items))
    for it in items:
        folder = str(it.get("folder") or "").encode("utf-8")
        name = str(it.get("name") or "").encode("utf-8")
        mtime = str(it.get("mtime") or "").encode("utf-8")
        data = item_bytes(it)
        for label, v in (("folder", folder), ("name", name), ("mtime", mtime)):
            if len(v) > 65535:
                raise PackError("%s 过长（上限 65535 字节）" % label)
        out += struct.pack(_HDR_FMT, len(folder), len(name), len(mtime), len(data), len(data))
        out += folder
        out += name
        out += mtime
        out += data
    return bytes(out)


def _parse_body(bb: bytes) -> list:
    if len(bb) < 4:
        raise PackError("包体被截断（连条数都读不到）")
    (n,) = struct.unpack(">I", bb[:4])
    off = 4
    items = []
    for i in range(n):
        if len(bb) < off + _HDR_SIZE:
            raise PackError("包体被截断（第 %d 条的条目头不完整）" % (i + 1))
        fl, nl, ml, size, dlen = struct.unpack(_HDR_FMT, bb[off:off + _HDR_SIZE])
        off += _HDR_SIZE
        if len(bb) < off + fl + nl + ml + dlen:
            raise PackError("包体被截断（第 %d 条数据不完整）" % (i + 1))
        folder = bb[off:off + fl].decode("utf-8", "surrogateescape"); off += fl
        name = bb[off:off + nl].decode("utf-8", "surrogateescape"); off += nl
        mtime = bb[off:off + ml].decode("utf-8", "surrogateescape"); off += ml
        data = bb[off:off + dlen]; off += dlen
        items.append({"name": name, "folder": folder, "mtime": mtime,
                      "size": size, "hash": sha_hex(data), "_data": data})
    if off != len(bb):
        raise PackError("包体尾部有 %d 字节多余数据（格式不一致）" % (len(bb) - off))
    return items


# ── 打包 / 拆包 ──────────────────────────────────────────────

def pack_bytes(k_pack: bytes, key_ref: str, items: list, created: str | None = None) -> bytes:
    """序列化。

    布局：
        MAGIC(8) VER(2) KRLEN(2) KREF(KRLEN) FRAME_SHA(32) HLEN(4) HSEALED(HLEN) BSEALED(rest)

    FRAME_SHA = sha256(header明文 ‖ key_ref) —— 把帧字段也纳入完整性保护。
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    header = {"v": VERSION, "created": created or now, "updated": now, "n": len(items)}
    hb = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    bb = _build_body(items)
    kr = key_ref.encode("utf-8")
    if len(kr) > 65535:
        raise PackError("key_ref 过长")

    frame_sha = hashlib.sha256(hb + kr).digest()
    h_sealed = vaultcrypto.seal(k_pack, hb)
    b_sealed = vaultcrypto.seal(k_pack, bb)

    out = bytearray()
    out += MAGIC
    out += struct.pack(">HH", VERSION, len(kr))
    out += kr
    out += frame_sha
    out += struct.pack(">I", len(h_sealed))
    out += h_sealed
    out += b_sealed
    return bytes(out)


def parse(blob: bytes) -> dict:
    """拆帧（不解密）。帧结构坏在这里就报，不拖到解密。"""
    head = len(MAGIC)
    if len(blob) < head + 4:
        raise PackError("不是 privpack 文件（长度不足）")
    if blob[:head] != MAGIC:
        raise PackError("不是 privpack 文件（magic 不符）")
    ver, krlen = struct.unpack(">HH", blob[head:head + 4])
    off = head + 4
    if len(blob) < off + krlen + 32 + 4:
        raise PackError("文件被截断（帧不完整）")
    key_ref = blob[off:off + krlen].decode("utf-8", "surrogateescape")
    off += krlen
    frame_sha = blob[off:off + 32]
    off += 32
    (hlen,) = struct.unpack(">I", blob[off:off + 4])
    off += 4
    if len(blob) < off + hlen:
        raise PackError("文件被截断（头不完整）")
    h_sealed = blob[off:off + hlen]
    off += hlen
    b_sealed = blob[off:]
    if len(b_sealed) < 28:
        raise PackError("文件被截断（体不完整或为空）")
    return {"version": ver, "key_ref": key_ref, "frame_sha": frame_sha,
            "h_sealed": h_sealed, "b_sealed": b_sealed}


def unpack(blob: bytes, k_pack: bytes) -> tuple:
    """解包：返回 (header_dict, items)。需要包密钥。

    items 每条带 `_data`（原始字节）与 `hash` —— 调用方可以逐条自证无损。
    """
    p = parse(blob)
    try:
        hb = vaultcrypto.open_sealed(k_pack, p["h_sealed"])
        bb = vaultcrypto.open_sealed(k_pack, p["b_sealed"])
    except Exception as e:
        raise PackError("解包失败（包密钥不对或包被改过）：%s: %s" % (type(e).__name__, str(e)[:60]))
    # 帧哈希要拿解出来的头回一次账 —— 这样"头被改"和"key_ref 被改"都能定位
    want = hashlib.sha256(hb + p["key_ref"].encode("utf-8", "surrogateescape")).digest()
    if want != p["frame_sha"]:
        raise PackError("帧校验失败：key_ref 或包头被改过（能解开但与帧哈希不符）")
    header = json.loads(hb.decode("utf-8"))
    return header, _parse_body(bb)


def peek(blob: bytes) -> dict:
    """不解密能看到什么（"锁定时屏幕上该显示什么"的判据）。

    故意只回结构与计数：**不解密就没有文件名**。
    """
    p = parse(blob)
    return {"ok": True, "version": p["version"],
            "key_ref_kind": "x25519" if vaultcrypto.is_box_ref(p["key_ref"]) else "master-wrap",
            "bytes": len(blob), "header_sealed": len(p["h_sealed"]),
            "body_sealed": len(p["b_sealed"])}


# ── 条目构造 ─────────────────────────────────────────────────

def make_item(name: str, data: bytes, folder: str = "", mtime: str | None = None) -> dict:
    """由一篇的明文造条目。`hash` 记明文哈希 —— 读出来能自证没被改过。"""
    return {"name": str(name), "folder": str(folder or ""),
            "mtime": mtime or time.strftime("%Y-%m-%dT%H:%M:%S"),
            "size": len(data), "hash": sha_hex(data), "_data": bytes(data)}


def item_bytes(it: dict) -> bytes:
    """取条目明文。内存条目直接用 `_data`；从 JSON 读回来的用 `b64`。"""
    if "_data" in it:
        return it["_data"]
    if "b64" in it:
        import base64
        return base64.b64decode(it["b64"])
    raise PackError("条目里没有正文")


def item_verify(it: dict) -> tuple:
    """返回 (是否无损, 说明)"""
    try:
        b = item_bytes(it)
    except Exception as e:
        return False, "取正文失败：%s" % str(e)[:40]
    if len(b) != it.get("size"):
        return False, "长度不符：%d vs %s" % (len(b), it.get("size"))
    if sha_hex(b) != it.get("hash"):
        return False, "哈希不符"
    return True, "无损"


# ── 原子落盘（沿用 tree_write 同一套规矩）─────────────────────

def write_file(path: str, blob: bytes, retries: int = 3) -> dict:
    """tmp + os.replace 原子替换（Windows 被占用时重试）。失败不静默。"""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".pp-tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    last = None
    for i in range(retries):
        try:
            os.replace(tmp, path)
            return {"ok": True, "path": path, "bytes": len(blob)}
        except OSError as e:
            last = e
            if i < retries - 1:
                time.sleep(0.15 * (i + 1))
    try:
        os.remove(tmp)
    except OSError:
        pass
    return {"ok": False, "why": "%s: %s" % (type(last).__name__, str(last)[:60])}


def read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()
