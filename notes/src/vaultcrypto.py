"""私密加锁的密码学底座（FORMAT §4.2）。

    密码 --scrypt--> 主密钥（只在内存）
    K_f（每文件随机）--AES-GCM(主密钥)--> key_ref          ← 老写法（要主密钥才能封）
    K_f --X25519 公钥封存--> key_ref "x25519:…"            ← 新写法（**锁着也能封**，2026-10-01）
    内容 --AES-GCM(K_f)--> content / 池文件

为什么要加"公钥封存"：辞玖 2026-10-01「设为私密不需要解锁」——
被保护的动作（加密）不该被锁挡住，而**读**私密当然还得要密码。
库持有一对 X25519：公钥明文存 manifest（只能加密，公开无害），私钥用主密钥包着存（要密码才解得开）。
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

KDF_ALGO = "scrypt"          # 参数写进 manifest，改参数后老库仍可解
KDF_N = 1 << 15
KDF_R = 8
KDF_P = 1
KEY_LEN = 32
VERIFY_PLAINTEXT = b"ai-notes/vault-unlock-check/v1"
ALGO = "aesgcm-v1"


class LockedError(Exception):
    """内容已加密且未解锁"""

    def __init__(self, file_id: str = ""):
        super().__init__("locked: %s" % (file_id or "-"))
        self.file_id = file_id


class BadPassword(Exception):
    pass


def new_salt() -> str:
    return base64.b64encode(os.urandom(16)).decode("ascii")


def derive(password: str, salt_b64: str, n: int = KDF_N, r: int = KDF_R, p: int = KDF_P) -> bytes:
    """密码 → 主密钥"""
    salt = base64.b64decode(salt_b64)
    kdf = Scrypt(salt=salt, length=KEY_LEN, n=int(n), r=int(r), p=int(p))
    return kdf.derive(password.encode("utf-8"))


def verify_blob(master: bytes) -> str:
    """验证块：库里只存它，不存密码"""
    nonce = os.urandom(12)
    return base64.b64encode(nonce + AESGCM(master).encrypt(nonce, VERIFY_PLAINTEXT, None)).decode("ascii")


def check_master(master: bytes, blob_b64: str) -> bool:
    try:
        raw = base64.b64decode(blob_b64)
        AESGCM(master).decrypt(raw[:12], raw[12:], None)
        return True
    except Exception:
        return False


def new_key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def wrap_key(master: bytes, key: bytes) -> str:
    """用主密钥包裹内容密钥（nonce 前置，base64 存库）"""
    nonce = os.urandom(12)
    return base64.b64encode(nonce + AESGCM(master).encrypt(nonce, key, None)).decode("ascii")


def unwrap_key(master: bytes, blob_b64: str) -> bytes:
    try:
        raw = base64.b64decode(blob_b64)
        return AESGCM(master).decrypt(raw[:12], raw[12:], None)
    except InvalidTag:
        raise BadPassword("内容密钥解不开（密码不对或库被改过）")


# ── 公钥封存盒（X25519 + HKDF + AES-GCM）2026-10-01 ─────────
PUB_PREFIX = "x25519:"
BOX_INFO = b"ai-notes/vault-box/v1"


def _x25519():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return X25519PrivateKey, X25519PublicKey, HKDF, hashes, serialization


def _raw(priv_or_pub) -> bytes:
    _, _, _, _, serialization = _x25519()
    if hasattr(priv_or_pub, "private_bytes"):
        return priv_or_pub.private_bytes(serialization.Encoding.Raw,
                                         serialization.PrivateFormat.Raw,
                                         serialization.NoEncryption())
    return priv_or_pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _shared(priv, pub) -> bytes:
    _, _, HKDF, hashes, _ = _x25519()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=BOX_INFO).derive(priv.exchange(pub))


def new_box_keypair() -> tuple:
    """库的封存盒密钥对：返回 (公钥 base64, 私钥原始字节)"""
    X25519PrivateKey, _, _, _, _ = _x25519()
    sk = X25519PrivateKey.generate()
    return base64.b64encode(_raw(sk.public_key())).decode("ascii"), _raw(sk)


def is_box_ref(key_ref) -> bool:
    return str(key_ref or "").startswith(PUB_PREFIX)


def wrap_box(key: bytes, pub_b64: str) -> str:
    """用库公钥把内容密钥封起来（**不需要主密钥** → 锁着也能设为私密）"""
    X25519PrivateKey, X25519PublicKey, _, _, _ = _x25519()
    eph = X25519PrivateKey.generate()
    pub = X25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
    shared = _shared(eph, pub)
    nonce = os.urandom(12)
    body = _raw(eph.public_key()) + nonce + AESGCM(shared).encrypt(nonce, key, None)
    return PUB_PREFIX + base64.b64encode(body).decode("ascii")


def open_box(priv_raw: bytes, blob: str) -> bytes:
    """用库私钥（要用主密钥解出来）打开公钥封存的内容密钥"""
    X25519PrivateKey, X25519PublicKey, _, _, _ = _x25519()
    if not priv_raw:
        raise LockedError("")
    raw = base64.b64decode(str(blob)[len(PUB_PREFIX):])
    eph_pub, nonce, ct = raw[:32], raw[32:44], raw[44:]
    shared = _shared(X25519PrivateKey.from_private_bytes(bytes(priv_raw)),
                     X25519PublicKey.from_public_bytes(eph_pub))
    try:
        return AESGCM(shared).decrypt(nonce, ct, None)
    except InvalidTag:
        raise BadPassword("内容密钥解不开（库密钥对不上或被改过）")


def open_key_ref(master: bytes, key_ref: str, priv_raw: bytes = None) -> bytes:
    """打开 key_ref：新写法要私钥（私钥本身用主密钥包着），老写法直接要主密钥。"""
    if is_box_ref(key_ref):
        return open_box(priv_raw, key_ref)
    if not master:
        raise LockedError("")
    return unwrap_key(master, key_ref)


def seal(key: bytes, plaintext: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def open_sealed(key: bytes, blob: bytes) -> bytes:
    return AESGCM(key).decrypt(blob[:12], blob[12:], None)
