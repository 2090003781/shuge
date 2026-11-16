"""私密加锁的端到端冒烟（走真实 HTTP API）。

跑法：python tools/smoke_lock.py [base] [--data <库的 data 目录>]
默认 base = http://127.0.0.1:8848，data = 默认库目录。

⚠ 这个脚本会**在目标库上临时设一个测试密码、跑完再还原**。对着真库跑时请确认没有别的写入者：
投递箱在库解锁期间会把 个人/私密 加密进库，用的是测试密码派生的主密钥 —— 等 kdf 被还原，
那几行就永远解不开了。run_all 走的是 tools/run_smoke_lock.py（临时库 + 临时 data）。

无论成败都会把库还原：目标笔记恢复原内容与原可见性，库密码参数恢复成测试前的样子。
（若还有文件停在加密态就**不还原密码参数**——那会把内容变成永久打不开。）
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

_ap = argparse.ArgumentParser()
_ap.add_argument("base", nargs="?", default="http://127.0.0.1:8848")
_ap.add_argument("--data", default=None, help="库的 data 目录（对着临时库跑时必须给，直接改库的那几处要用）")
_a = _ap.parse_args()
BASE = _a.base.rstrip("/")
DATA = _a.data
PW = "smoke-lock-9527"
ok_all = True

sys.path.insert(0, "src")
from db import Database          # noqa: E402


def _db():
    """直接读库时用哪个 data 目录：给了 --data 就用它（临时库），否则默认库"""
    return Database(DATA) if DATA else Database()

KDF_KEYS = ("kdf_algo", "kdf_salt", "kdf_n", "kdf_r", "kdf_p", "verify_blob",
            "vault_pub", "vault_priv_ref")     # 库密钥对也在锁状态里（2026-10-01）


def call(path, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra)) if extra else ""))


def walk(n, out):
    out.extend(n.get("files") or [])
    for c in (n.get("children") or []):
        walk(c, out)
    return out


def snapshot():
    d = _db()
    man = d.manifest()
    snap = {k: man.get(k, "") for k in KDF_KEYS}
    d.close()
    return snap


def restore(snap, fid, orig, orig_vis, unlock_pw=None):
    """把库还原：删掉临时笔记 + 恢复密码参数"""
    d = _db()
    row = d.file_by_id(fid)
    if row and row["enc_algo"]:
        d.set_manifest(**snap)
        d.close()
        print("  ⚠ 临时笔记仍是加密态，只还原密码参数（不留私密垃圾）")
        d = _db()
    if d.file_by_id(fid):
        d.delete_file(fid)                      # 自建自清：连块带向量一起走
    d.set_manifest(**snap)
    d.close()


snap = snapshot()
files_before = None

files_before = call("/api/health")[1]["stats"]["files"]

print("① 锁状态")
st, lock = call("/api/lock")
check("接口可用", st == 200 and lock.get("ok"), lock)
check("冷启动是锁定态", lock.get("unlocked") is False, lock)

# 用自建的临时笔记做被测对象：上锁会清派生层，拿真笔记测会留下向量缺口
PROBE = "ZZLOCKPROBE"
orig = "# 锁冒烟\n\n%s 只在这篇里。\n" % PROBE
made = call("/api/files", "POST", {"name": "zz-冒烟-锁.md", "content": orig})[1]
fid = made["file_id"]
orig_vis = "internal"
probe = PROBE


def search(term):
    return call("/api/search?q=%s&limit=30" % urllib.parse.quote(term))[1]["hits"]


check("明文时能搜到自己", any(h["file_id"] == fid for h in search(probe)), probe)

try:
    print("② 设库密码")
    if not lock.get("configured"):
        st, r = call("/api/lock/set", "POST", {"password": PW, "confirm": PW})
        check("设置成功并解锁", st == 200 and r.get("unlocked"), r)
        st, r = call("/api/lock/set", "POST", {"password": "x", "confirm": "x"})
        check("重复设置被拒", st == 400, r.get("error"))
    else:
        st, r = call("/api/lock/unlock", "POST", {"password": PW})
        check("解锁成功", st == 200, r.get("error"))

    print("③ 设为私密：落盘即加密")
    st, r = call("/api/files/%s/visibility" % fid, "POST", {"visibility": "private"})
    check("切换成功", st == 200 and r["result"].get("encrypted"), r)
    _, f = call("/api/files/" + fid)
    check("解锁态仍读到原文", f["file"]["content"] == orig)
    check("元数据标 private", f["file"]["visibility"] == "private")
    mine = [h for h in search(probe) if h["file_id"] == fid]
    check("解锁态仍可检索但标敏感", bool(mine) and all(h.get("is_sensitive") for h in mine),
          "%d 条" % len(mine))

    print("③b 私密篇不留前像（前像是明文落盘的）2026-09-28")
    # 前像是解密后的明文落在 data/前像/；私密篇的意义就是"磁盘上是密文"，
    # 给它留前像等于在库目录里再摊一份明文。代价：私密篇删除/覆盖后无法从回收站恢复。
    st, r3 = call("/api/files/%s" % fid, "PUT", {"content": orig + "\n再写一句。\n"})
    _sv = r3.get("saved") if isinstance(r3.get("saved"), dict) else {}
    bi3 = (_sv or {}).get("before_image") or {}
    check("覆盖私密篇：明确跳过前像（不是静默失败）",
          bi3.get("ok") is False and bi3.get("why") == "private-skipped", bi3)
    st, lst = call("/api/before-images?file_id=%s" % fid)
    check("回收站里找不到它", not (lst.get("images") or []), lst.get("images"))
    st, r4 = call("/api/files/%s" % fid, "PUT", {"content": orig})   # 写回原文，后面还按原文比
    check("写回原文成功", st == 200 and r4.get("ok"), r4.get("error"))

    print("④ 上锁：读不到内容、也改不了")
    st, r = call("/api/lock/lock", "POST", {})
    check("上锁成功", st == 200 and r.get("unlocked") is False, r)
    check("上锁后退出检索", not any(h["file_id"] == fid for h in search(probe)))
    # 2026-09-28：上锁后私密篇「整体隐藏」—— 按 id 也读不到、连名字都不给
    #（和 /text、/raw 一个口径；辞玖："上锁直接隐藏上锁文件"）
    st, f = call("/api/files/" + fid)
    check("私密篇整体隐藏（按 id 也读不到）", st == 403 and f.get("locked") is True, f)
    st, r = call("/api/files/" + fid, "PUT", {"content": "想偷偷改"})
    check("锁定态拒绝保存", st == 403 and r.get("locked"), r.get("error"))

    print("⑤ 错误密码 / 正确密码")
    st, r = call("/api/lock/unlock", "POST", {"password": "definitely-wrong"})
    check("错密码被拒", st == 403, r.get("error"))
    st, r = call("/api/lock/unlock", "POST", {"password": PW})
    check("对密码解锁", st == 200 and r.get("unlocked"), r)
    check("解锁后补建索引", r.get("reindexed", 0) >= 1, r)
    _, f = call("/api/files/" + fid)
    check("内容还原", f["file"]["content"] == orig)
    mine = [h for h in search(probe) if h["file_id"] == fid]
    check("解锁后可被检索到", bool(mine), "%d 条 | 探针 %r" % (len(mine), probe))
    check("命中带 is_sensitive", all(h.get("is_sensitive") for h in mine) if mine else False,
          mine[0].get("is_sensitive") if mine else None)

    print("⑥ 取消私密：解密回明文")
    st, r = call("/api/files/%s/visibility" % fid, "POST", {"visibility": orig_vis})
    check("切换成功", st == 200 and r["result"].get("decrypted"), r)
    _, f = call("/api/files/" + fid)
    check("可见性还原", f["file"]["visibility"] == orig_vis)
    check("内容仍是原文", f["file"]["content"] == orig)
finally:
    print("⑥c 锁定时：引用到私密篇的那处引用必须「失效」（不露名字、标成 hidden）")
    st, c = call("/api/files", "POST", {"name": "zz-冒烟-被引.md", "content": "# 被引\n\n随便写点。\n"})
    cid = c["file_id"]
    st, _ = call("/api/files/%s" % fid, "PUT",
                 {"content": orig + "\n见 @zz-冒烟-被引 这篇。\n"})
    st, _ = call("/api/files/%s/visibility" % cid, "POST", {"visibility": "private"})
    check("被测篇设成私密", st == 200, st)
    _, f = call("/api/files/" + fid)
    mine = [m for m in (f["file"]["mentions"] or []) if m.get("id") == cid]
    check("解锁态：引用照常（能看见名字、不标 hidden）",
          bool(mine) and not mine[0].get("hidden") and mine[0].get("name") == "zz-冒烟-被引.md",
          mine[0] if mine else None)
    call("/api/lock/lock", "POST", {})
    _, f2 = call("/api/files/" + fid)
    hm = [m for m in (f2["file"]["mentions"] or []) if m.get("id") == cid]
    check("锁上后：那条引用还在（用 id 认得出）", bool(hm), f2["file"]["mentions"])
    check("锁上后：名字换成占位（不露私密篇名）",
          bool(hm) and hm[0].get("name") == "（私密·未解锁）", hm[0] if hm else None)
    check("锁上后：标成 hidden 且 anchor 清空（客户端据此渲染成不可点）",
          bool(hm) and hm[0].get("hidden") is True and not hm[0].get("anchor"), hm[0] if hm else None)
    check("锁上后：正文里的那行字还在（引用是文本，不是链接对象）",
          "@zz-冒烟-被引" in f2["file"]["content"], True)
    st, r = call("/api/lock/unlock", "POST", {"password": PW})
    check("解锁回来", st == 200 and r.get("unlocked"), r.get("error"))
    _, f3 = call("/api/files/" + fid)
    m3 = [m for m in (f3["file"]["mentions"] or []) if m.get("id") == cid]
    check("解锁后：名字恢复、不再标 hidden",
          bool(m3) and not m3[0].get("hidden") and m3[0].get("name") == "zz-冒烟-被引.md",
          m3[0] if m3 else None)
    # 写回原文（后面按原文比）。注意要带 allow_shrink：这版比上一版短，会被"缩短闸"拦下
    st, rr = call("/api/files/%s" % fid, "PUT", {"content": orig, "allow_shrink": True})
    check("探针正文写回原文", st == 200 and rr.get("ok"), rr.get("error"))
    call("/api/files/%s/visibility" % cid, "POST", {"visibility": "internal"})
    call("/api/files/" + cid, "DELETE")

    print("⑥b 换密码 / 取消密码（2026-09-29）")
    NEWPW = "smoke-lock-9527b"
    st, r = call("/api/files/%s/visibility" % fid, "POST", {"visibility": "private"})
    check("探针重新设成私密", st == 200 and r["result"].get("encrypted"), r)
    st, r = call("/api/lock/change", "POST", {"password": NEWPW, "confirm": NEWPW})
    check("换密码成功并报重新加密了几篇", st == 200 and r.get("reencrypted") == 1, r)
    st, r = call("/api/lock/change", "POST", {"password": "xx", "confirm": "xx"})
    check("新密码太短被拒（400）", st == 400, r.get("error"))
    st, r = call("/api/lock/change", "POST", {"password": NEWPW, "confirm": "别的不一样"})
    check("两次不一致被拒（400）", st == 400, r.get("error"))
    call("/api/lock/lock", "POST", {})
    st, r = call("/api/lock/unlock", "POST", {"password": PW})
    check("旧密码已失效", st == 403, r.get("error"))
    st, r = call("/api/lock/unlock", "POST", {"password": NEWPW})
    check("新密码能解锁", st == 200 and r.get("unlocked"), r.get("error"))
    _, f = call("/api/files/" + fid)
    check("换密码后正文还是原文（逐字）", f["file"]["content"] == orig, repr(f["file"]["content"][:40]))
    check("仍然是私密", f["file"]["visibility"] == "private", f["file"]["visibility"])

    st, r = call("/api/lock/clear", "POST", {})
    check("取消密码不带 confirm → 400", st == 400, r.get("error"))
    st, r = call("/api/lock/clear", "POST", {"confirm": True})
    check("取消密码成功并报解密了几篇", st == 200 and r.get("decrypted") == 1, r)
    check("取消后 configured=false / unlocked=false",
          r.get("configured") is False and r.get("unlocked") is False, r)
    _, f = call("/api/files/" + fid)
    check("那篇还在、正文还是原文", f["file"]["content"] == orig, repr(f["file"]["content"][:40]))
    check("可见性变成 internal（不再隐藏）", f["file"]["visibility"] == "internal", f["file"]["visibility"])
    st, r = call("/api/lock/clear", "POST", {"confirm": True})
    check("再取消一次 → 400（本来就没密码）", st == 400, r.get("error"))

    print("⑥d 锁着也能「设为私密」+ 批量切可见性（辞玖 2026-10-01 13:00）")
    PLAIN = "# 锁定私密\n\n这一段是要被加密保护的正文。\n"
    libdir = os.path.join(tempfile.gettempdir(), "zz-locklib-%d" % int(time.time()))
    _dd = _db(); _dd.set_manifest(library_dir=libdir); _dd.close()

    # ① 没设过密码：做不了（没有钥匙能保护它）→ 明说"先设密码"
    st, m = call("/api/files", "POST", {"name": "zz-冒烟-锁定私密.md", "content": PLAIN})
    fid3 = m.get("file_id")
    st, r = call("/api/files/%s/visibility" % fid3, "POST", {"visibility": "private"})
    check("没设过密码时设为私密 → 403 且带 need_setup", st == 403 and r.get("need_setup") is True, r)
    check("这时 error 是人话（提「密码」），不是裸的 locked",
          r.get("error") != "locked" and "密码" in str(r.get("error")), r.get("error"))

    # ② 设密码 → manifest 里该有一对库密钥
    st, r = call("/api/lock/set", "POST", {"password": PW, "confirm": PW})
    check("设密码成功", st == 200 and r.get("unlocked"), r.get("error"))
    _d1 = _db(); man = _d1.manifest(); _d1.close()
    check("manifest 里有库公钥（明文，只能加密）", bool(man.get("vault_pub")), str(man.get("vault_pub"))[:20])
    check("私钥是用主密钥包着的（不是明文躺在库里）",
          bool(man.get("vault_priv_ref")) and man.get("vault_priv_ref") != man.get("vault_pub"), '')

    # ③ **上锁**之后设为私密：应当成功，走公钥那条路
    call("/api/lock/lock", "POST", {})
    st, r = call("/api/files/%s/visibility" % fid3, "POST", {"visibility": "private"})
    check("🔑 锁着时设为私密 → 200 且 via=pubkey",
          st == 200 and (r.get("result") or {}).get("via") == "pubkey", r)

    d2 = _db()
    row = d2.file_by_id(fid3)
    raw = d2.conn.execute("SELECT content FROM files WHERE id=?", (fid3,)).fetchone()["content"] or b""
    nch = d2.conn.execute("SELECT COUNT(*) c FROM chunks WHERE file_id=?", (fid3,)).fetchone()["c"]
    d2.close()
    check("库里那篇是密文态（enc_algo + x25519 封存引用）",
          row["enc_algo"] == "aesgcm-v1" and str(row["key_ref"]).startswith("x25519:"),
          [row["enc_algo"], str(row["key_ref"])[:12]])
    check("落库的是密文（明文不在里面）", (PLAIN.encode() not in raw) and len(raw) > 16, len(raw))
    check("派生层（明文块）当场清掉了 —— 否则「私密」只是改了个标记", nch == 0, nch)
    pth = os.path.join(libdir, "zz-冒烟-锁定私密.md")
    on_disk = open(pth, "rb").read() if os.path.isfile(pth) else b""
    check("库文件夹里落的是密文（锁着也落得下去）",
          bool(on_disk) and PLAIN.encode() not in on_disk, len(on_disk))

    # ④ 锁着看不见它
    st, _r = call("/api/files/%s" % fid3)
    check("锁着读它 → 403/404（看不见）", st in (403, 404), st)

    # ⑤ **解锁后能读回原文** —— 这条最关键
    st, r = call("/api/lock/unlock", "POST", {"password": PW})
    check("解锁回来", st == 200 and r.get("unlocked"), r.get("error"))
    _, f = call("/api/files/" + fid3)
    check("🔓 解锁后读回原文（逐字一致）", f["file"]["content"] == PLAIN, repr(f["file"]["content"][:40]))

    # ⑥ 锁着"移出私密"要挡（解密要密码），解锁后能移
    call("/api/lock/lock", "POST", {})
    st, r = call("/api/files/%s/visibility" % fid3, "POST", {"visibility": "internal"})
    check("锁着移出私密 → 403 locked（要解密，必须解锁）", st == 403 and r.get("locked") is True, r.get("hint"))
    call("/api/lock/unlock", "POST", {"password": PW})
    st, r = call("/api/files/%s/visibility" % fid3, "POST", {"visibility": "internal"})
    check("解锁后移出私密 → 200 且解密了", st == 200 and (r.get("result") or {}).get("decrypted"), r)
    _, f = call("/api/files/" + fid3)
    check("移出私密后正文还是原文", f["file"]["content"] == PLAIN, repr(f["file"]["content"][:30]))

    # ⑦ 批量：三篇 → 锁着一次全设私密 → 锁着不许移出 → 解锁一次全移出
    batch = []
    for k in (1, 2, 3):
        st, mm = call("/api/files", "POST",
                      {"name": "zz-冒烟-批量%d.md" % k, "content": "# 批%d\n\n%s" % (k, PLAIN)})
        batch.append(mm.get("file_id"))
    call("/api/lock/lock", "POST", {})
    st, r = call("/api/visibility/batch", "POST", {"ids": batch, "visibility": "private"})
    check("🔑 锁着批量设为私密 → 3 篇都成", st == 200 and r.get("count") == 3, r)
    d3 = _db()
    vis = [d3.file_by_id(x)["visibility"] for x in batch]
    refs = [str(d3.file_by_id(x)["key_ref"])[:7] for x in batch]
    d3.close()
    check("三篇在库里都是 private + 公钥封存",
          vis == ["private"] * 3 and refs == ["x25519:"] * 3, [vis, refs])
    st, r = call("/api/visibility/batch", "POST", {"ids": batch, "visibility": "internal"})
    check("锁着批量移出私密 → 403 locked（要解密）", st == 403 and r.get("locked") is True, r.get("hint"))
    call("/api/lock/unlock", "POST", {"password": PW})
    st, r = call("/api/visibility/batch", "POST", {"ids": batch, "visibility": "internal"})
    check("解锁后批量移出私密 → 3 篇都成", st == 200 and r.get("count") == 3, r)
    _, f2 = call("/api/files/" + batch[0])
    check("批量移出后正文还是原文（锁着加密 → 解锁读得回来）",
          PLAIN in f2["file"]["content"], repr(f2["file"]["content"][:40]))

    # ⑧ 老库（今天之前设的密码：manifest 里没有库公钥）→ 解锁时应当自动补上
    print("  · 老库自愈：解锁时自动补库密钥对")
    st, m = call("/api/files", "POST", {"name": "zz-冒烟-老库.md", "content": PLAIN})
    fid4 = m.get("file_id")
    _d5 = _db()
    _d5.set_manifest(vault_pub="", vault_priv_ref="")        # 装成"老库"：只有密码，没有库密钥对
    _d5.close()
    call("/api/lock/lock", "POST", {})
    st, r = call("/api/files/%s/visibility" % fid4, "POST", {"visibility": "private"})
    check("老库 + 锁着：暂时做不了，但**提示是「先解锁一次」**（不是「先设密码」）",
          st == 403 and r.get("pub_ready") is False and r.get("need_setup") is False
          and "解锁" in str(r.get("error")), r)
    st, r = call("/api/lock/unlock", "POST", {"password": PW})
    check("解锁那一刻自动补上了库密钥对", st == 200 and r.get("pub_ready_made") is True
          and r.get("pub_ready") is True, {k: r.get(k) for k in ("pub_ready_made", "pub_ready")})
    call("/api/lock/lock", "POST", {})
    st, r = call("/api/files/%s/visibility" % fid4, "POST", {"visibility": "private"})
    check("🔑 补完之后：锁着也能设为私密", st == 200 and (r.get("result") or {}).get("via") == "pubkey", r)
    call("/api/lock/unlock", "POST", {"password": PW})
    _, f4 = call("/api/files/" + fid4)
    check("老库补的那一对也能读回原文", f4["file"]["content"] == PLAIN, repr(f4["file"]["content"][:30]))
    call("/api/files/%s/visibility" % fid4, "POST", {"visibility": "internal"})

    # 收尾
    for x in batch + [fid3, fid4]:
        call("/api/files/" + x, "DELETE")
    call("/api/lock/clear", "POST", {"confirm": True})
    _d9 = _db(); _d9.set_manifest(library_dir=""); _d9.close()
    shutil.rmtree(libdir, ignore_errors=True)

    print("⑦ 还原库状态")
    restore(snap, fid, orig, orig_vis)
    call("/api/lock/lock", "POST", {})
    _, lock = call("/api/lock")
    check("密码参数回到测试前", lock.get("configured") == bool(snap.get("kdf_salt")), lock)
    hh = call("/api/health")[1]
    check("库规模复原", hh["stats"]["files"] == files_before,
          "%s vs %s" % (hh["stats"]["files"], files_before))
    # 只断言"自己那篇"清干净：新建/改动的文件本来就等向量索引器补建，
    # 全局 pending 数会被别的测试影响（顺序相关），不是本篇该管的
    d = _db()
    left_chunks = d.conn.execute("SELECT COUNT(*) c FROM chunks WHERE file_id=?", (fid,)).fetchone()["c"]
    left_vecs = d.conn.execute(
        "SELECT COUNT(*) c FROM vectors WHERE chunk_id LIKE ?", (fid + ":%",)).fetchone()["c"]
    d.close()
    check("临时笔记的块与向量都清干净", left_chunks == 0 and left_vecs == 0,
          "块 %d / 向量 %d" % (left_chunks, left_vecs))

print("\n========  结论：%s" % ("全部通过" if ok_all else "有失败项"))
sys.exit(0 if ok_all else 1)
