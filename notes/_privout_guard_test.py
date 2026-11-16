# -*- coding: utf-8 -*-
"""测 priv_out.move_out —— **直接调函数**，不碰真包、不碰真库树。

重点：「顺序保证」必须能被**真验** ——
  在临时库里建真包 → 把 to_dir 指到**不可用**路径 → 调 move_out
  → 断言返回失败，且**包文件 sha256 一个 bit 都没变**。

说明：本环境拿不到库密码，所以测试用替身**只替掉"取条目"这一步**
（`pf.read_items`）；被测的"落盘失败 → 不删包里那条"完全走真代码。
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

spec = importlib.util.spec_from_file_location("priv_out", r"D:\core-notes\tools\priv_out.py")
po = importlib.util.module_from_spec(spec)
spec.loader.exec_module(po)

import priv_folder as pf  # noqa: E402
import privpack as pp  # noqa: E402
import vaultcrypto  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-50s %s" % ("✓" if cond else "✗", name, detail))


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def build_lib(td, items):
    """在 td 下造一个「库」：只有私密文件夹 + 一个真包"""
    lib = os.path.join(td, "库")
    os.makedirs(os.path.join(lib, "个人", "私密"), exist_ok=True)
    k = vaultcrypto.new_key()
    pub, priv = vaultcrypto.new_box_keypair()
    key_ref = pp.seal_key(k, pub)
    pp.write_file(pf.pack_abs(lib), pp.pack_bytes(k, key_ref, items))
    return lib, k, pub, priv


print("=" * 78)
print("测 priv_out.move_out（真跑代码路径）")
print("=" * 78)

# ── ① 落盘失败 → 包一个 bit 都不能变（核心保证）──────────────
print("① 落盘失败时：包必须一个 bit 都不变")
with tempfile.TemporaryDirectory(prefix="mo-fail-") as td:
    items = [pp.make_item("要移出.md", "第一篇".encode("utf-8")),
             pp.make_item("留守.md", "第二篇".encode("utf-8"))]
    lib, k, pub, priv = build_lib(td, items)
    pack = pf.pack_abs(lib)
    h0 = sha(pack)
    blocker = os.path.join(td, "文件不是目录")
    open(blocker, "w").close()

    real_read = pf.read_items
    pf.read_items = lambda *a, **kw: items          # 只替"取条目"，其余全真
    try:
        r = po.move_out(lib, master=b"x", priv_raw=priv, name="要移出.md",
                        to_dir=blocker, folder="", keep=False, vault_pub=pub)
    finally:
        pf.read_items = real_read

    check("move_out 返回失败", r.get("ok") is False, (r.get("why") or "")[:56])
    check("没有改包（pack_rewritten=False）", r.get("pack_rewritten") is False)
    check("包文件 sha256 一个 bit 都没变", sha(pack) == h0)
    check("包内仍是 2 条", r.get("left") == 2, "left=%s" % r.get("left"))
    check("包仍可解且两条都在", len(pp.unpack(pp.read_file(pack), k)[1]) == 2)
print()

# ── ② 成功路径：落盘 + 包里删条 ──────────────────────────────
print("② 成功路径：落盘成普通笔记 + 包里删掉该条")
with tempfile.TemporaryDirectory(prefix="mo-ok-") as td:
    items = [pp.make_item("要移出.md", "第一篇".encode("utf-8")),
             pp.make_item("留守.md", "第二篇".encode("utf-8"))]
    lib, k, pub, priv = build_lib(td, items)
    outdir = os.path.join(td, "落盘处")

    real_read = pf.read_items
    calls = {"n": 0}

    def fake_read(l, m, p):
        # 第一次给"解密得到"的 2 条；之后 re-read 走真实现（复查那一步）
        calls["n"] += 1
        if calls["n"] == 1:
            return items
        return real_read(l, m, p)

    pf.read_items = fake_read
    try:
        r = po.move_out(lib, master=b"x", priv_raw=priv, name="要移出.md",
                        to_dir=outdir, folder="", keep=False, vault_pub=pub)
    finally:
        pf.read_items = real_read

    check("move_out 成功", r.get("ok") is True, r.get("why", ""))
    check("包已重写", r.get("pack_rewritten") is True)
    dst = r.get("path") or ""
    check("落盘文件存在", bool(dst) and os.path.isfile(dst), os.path.basename(dst))
    if dst and os.path.isfile(dst):
        check("落盘内容与原字节一致", open(dst, "rb").read() == "第一篇".encode("utf-8"))
    check("剩 1 条", r.get("left") == 1, "left=%s" % r.get("left"))
    got = pp.unpack(pp.read_file(pf.pack_abs(lib)), k)[1]
    check("包里只剩留守那篇", [x["name"] for x in got] == ["留守.md"])
    check("留守内容完好", pf.get_item_bytes(got, "留守.md") == "第二篇".encode("utf-8"))
print()

# ── ③ keep 模式：只落盘不删 ─────────────────────────────────
print("③ --keep：只落盘，包里不动")
with tempfile.TemporaryDirectory(prefix="mo-keep-") as td:
    items = [pp.make_item("要移出.md", "第一篇".encode("utf-8"))]
    lib, k, pub, priv = build_lib(td, items)
    h0 = sha(pf.pack_abs(lib))
    outdir = os.path.join(td, "落盘处")
    real_read = pf.read_items
    pf.read_items = lambda *a, **kw: items
    try:
        r = po.move_out(lib, master=b"x", priv_raw=priv, name="要移出.md",
                        to_dir=outdir, folder="", keep=True, vault_pub=pub)
    finally:
        pf.read_items = real_read
    check("成功", r.get("ok") is True)
    check("包未重写", r.get("pack_rewritten") is False)
    check("包 sha256 未变", sha(pf.pack_abs(lib)) == h0)
    check("落盘文件在", os.path.isfile(r.get("path") or ""))
    check("包里仍是 1 条", r.get("left") == 1)
print()

# ── ④ 包里没有那一篇 → 明确失败 ─────────────────────────────
print("④ 包里没有那一篇 → 明确报错")
with tempfile.TemporaryDirectory(prefix="mo-miss-") as td:
    items = [pp.make_item("别的.md", b"x")]
    lib, k, pub, priv = build_lib(td, items)
    h0 = sha(pf.pack_abs(lib))
    real_read = pf.read_items
    pf.read_items = lambda *a, **kw: items
    try:
        r = po.move_out(lib, master=b"x", priv_raw=priv, name="不存在.md",
                        to_dir=os.path.join(td, "out"), vault_pub=pub)
    finally:
        pf.read_items = real_read
    check("失败且说清原因", r.get("ok") is False and "没有这一篇" in (r.get("why") or ""),
          (r.get("why") or "")[:50])
    check("包未动", sha(pf.pack_abs(lib)) == h0)
print()

print("=" * 78)
print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("   ✗ %s" % f)
if not FAIL:
    print("★ move_out 成立：**落盘失败时包一个 bit 都不变**（顺序保证真验过）")
print("=" * 78)
sys.exit(0 if not FAIL else 1)
