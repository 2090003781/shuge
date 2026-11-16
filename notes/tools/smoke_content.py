"""按路径取库内正文的冒烟：单条 / 批量 / 老路径兼容 / 不存在 / 截断 / 私有锁定。"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
ok_all = True


def call(path, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:140] if extra else ""))


# 找一篇真实文件当样本
tree = call("/api/tree")[1]["tree"]


def walk(n, prefix, out):
    for f in (n.get("files") or []):
        out.append((prefix + f["name"], f["name"]))
    for c in (n.get("children") or []):
        walk(c, prefix + c["name"] + "/", out)
    return out


files = walk(tree, "", [])
# 样本优先选 vault/ 下的：只有它的"老写法"（去掉 vault/ 前缀）才跟原路径不同。
# 选到根目录文件的话两个路径一模一样，批量返回只有 2 个键（旧写法会假失败，2026-09-26）
sample_path, sample_name = next((f for f in files if f[0].startswith("vault/")), files[0])
print("① 样本：%s" % sample_path)

st, r = call("/api/content?path=" + urllib.parse.quote(sample_path))
it = r["item"]
check("单条取到", st == 200 and it["found"], it.get("error"))
check("有正文", bool(it.get("content")))
check("chars 与正文长度一致（未截断时）", it["chars"] == len(it["content"]) or it.get("truncated"),
      "%s vs %s" % (it["chars"], len(it["content"])))

print("② 老写法（去掉 vault/ 前缀）也要认")
legacy = sample_path[len("vault/"):] if sample_path.startswith("vault/") else sample_path
st, r2 = call("/api/content?path=" + urllib.parse.quote(legacy))
check("兼容老路径", r2["item"]["found"] and r2["item"]["path"] == sample_path, r2["item"])

print("③ 截断")
st, r3 = call("/api/content?path=%s&max=10" % urllib.parse.quote(sample_path))
check("按 max 截断并打标记", len(r3["item"]["content"]) <= 10 and r3["item"].get("truncated"),
      len(r3["item"]["content"]))

print("④ 不存在")
st, r4 = call("/api/content?path=" + urllib.parse.quote("vault/根本没有这个文件.md"))
check("取不到时 found=false 而不是报错", st == 200 and r4["item"]["found"] is False, r4["item"].get("error"))

print("⑤ 批量")
st, r5 = call("/api/content/batch", "POST", {"paths": [sample_path, legacy, "vault/没有.md"], "max": 20})
items = r5["items"]
check("三条各有着落", len(items) == 3)
check("批量内容也截断", all(len(v.get("content", "")) <= 20 for v in items.values()))
check("批量能认出不存在的那条", items["vault/没有.md"]["found"] is False)

print("⑥ 私有文件：未解锁时不给正文")
# ⚠ 这一段要"自己设密码、跑完再清掉"。**库已经设过密码时绝对不跑** ——
# 2026-09-27 事故：这里以前在 finally 里无条件 `set_manifest(kdf_salt="", …)`，
# 把辞玖刚设的库密码连同密钥材料一起抹了（私密篇于是再也读不出来）。
if call("/api/lock")[1].get("configured"):
    print("   （库已设密码 → 跳过这一段：绝不动主人的密钥材料）")
else:
    st, made = call("/api/files", "POST", {"name": "zz-体检-私有.md", "content": "# 私有\n\n不该被读到。\n"})
    fid = made["file_id"]
    sys.path.insert(0, "src")
    from db import Database
    d = Database()
    KDF_KEYS = ("kdf_algo", "kdf_salt", "kdf_n", "kdf_r", "kdf_p", "verify_blob")
    snap = {k: (d.manifest() or {}).get(k, "") for k in KDF_KEYS}      # 开工前的密钥材料（照它还原）
    try:
        call("/api/lock/set", "POST", {"password": "smoke-9527", "confirm": "smoke-9527"})
        call("/api/files/%s/visibility" % fid, "POST", {"visibility": "private"})
        call("/api/lock/lock", "POST", {})
        st, r6 = call("/api/content?path=" + urllib.parse.quote("zz-体检-私有.md"))
        check("未解锁时 locked=true 且无正文", r6["item"]["locked"] is True and not r6["item"]["content"], r6["item"])
        call("/api/lock/unlock", "POST", {"password": "smoke-9527"})
        st, r7 = call("/api/content?path=" + urllib.parse.quote("zz-体检-私有.md"))
        check("解锁后能取到正文", r7["item"]["found"] and "不该被读到" in r7["item"]["content"], r7["item"].get("locked"))
    finally:
        call("/api/files/%s/visibility" % fid, "POST", {"visibility": "internal"})
        call("/api/files/" + fid, "DELETE")
        call("/api/lock/lock", "POST", {})
        d.set_manifest(**snap)                    # 只还原成**开工前**那份（原来是空才清空）
        d.close()
        check("密钥材料还原成开工前", call("/api/lock")[1]["configured"] == bool(snap.get("kdf_salt")))

print("\n========  结论：%s" % ("全部通过" if ok_all else "有失败项"))
sys.exit(0 if ok_all else 1)
