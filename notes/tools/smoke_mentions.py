"""@ 提及双链的端到端冒烟（走真实 HTTP API，不碰内部函数）。

跑法：python tools/smoke_mentions.py [base]
默认 base = http://127.0.0.1:8848

测完会把自己造的测试笔记删掉（不留垃圾、不留孤儿边）。
"""
import json
import sys
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
ok_all = True


def call(path, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra)) if extra else ""))


files_before = json.load(urllib.request.urlopen(BASE + "/api/health", timeout=60))["stats"]["files"]

print("① 取两篇真实笔记当被引用目标")
tree = call("/api/tree")


def walk(n, out):
    out.extend(n.get("files") or [])
    for c in (n.get("children") or []):
        walk(c, out)
    return out


files = walk(tree["tree"], [])
check("树里有文件", len(files) > 0, "%d 篇" % len(files))
target = files[0]
tname = target["name"][:-3] if target["name"].lower().endswith(".md") else target["name"]
print("     目标：《%s》（id=%s）" % (target["name"], target["id"][:8]))

print("② 新建一篇带 @ 提及的测试笔记")
body = "# 双链冒烟\n\n先看 @%s ，再看 @不存在的笔记XYZ ，邮箱不算 a@b.com。\n\n```\n@代码块里的不算\n```\n" % tname
created = call("/api/files", "POST", {"name": "zz-冒烟-双链.md", "content": body})
fid = created["file_id"]
check("新建成功", bool(fid), fid[:8])

print("③ 保存（触发解析 + 写边）")
saved = call("/api/files/" + fid, "PUT", {"content": body})
sv = saved.get("saved", {})
check("写了 1 条提及边", sv.get("mentions") == 1, sv)
check("没解析上的锚点被报出来", sv.get("unresolved") == ["不存在的笔记XYZ"], sv.get("unresolved"))

print("④ 读回来：mentions 带对方当前名")
got = call("/api/files/" + fid)["file"]
ms = got.get("mentions") or []
check("mentions 有 1 条", len(ms) == 1, ms)
check("锚点是输入的短名", ms and ms[0]["anchor"] == tname, ms and ms[0]["anchor"])
check("名字来自 files 表（可跟随改名）", ms and ms[0]["name"] == target["name"], ms and ms[0]["name"])
check("指向正确的文件", ms and ms[0]["id"] == target["id"])

print("⑤ 目标文件的反向链接里有这篇")
back = call("/api/files/" + target["id"])["file"].get("backlinks") or []
mine = [b for b in back if b["id"] == fid]
check("反向链接出现", len(mine) == 1, back)

print("⑥ 删掉提及 → 边跟着消失（关系是数据，不是文本残渣）")
# 故意把正文改短（删掉提及）：短缩闸对脚本要“明说”（人点保存才不问）
saved2 = call("/api/files/" + fid, "PUT",
              {"content": "# 双链冒烟\n\n没有提及了。\n", "allow_shrink": True})
check("提及边归零", saved2.get("saved", {}).get("mentions") == 0, saved2.get("saved"))
got2 = call("/api/files/" + fid)["file"]
check("mentions 空", not got2.get("mentions"), got2.get("mentions"))

print("⑦ 删掉测试笔记 → 不留孤儿边")
call("/api/files/" + fid, "DELETE")
health = call("/api/health")
check("文件数回到测试前", health["stats"]["files"] == files_before,
      "%s vs %s" % (health["stats"]["files"], files_before))

print("⑧ 重新引用一次，然后由目标侧看反向链接（再清一次）")
created2 = call("/api/files", "POST", {"name": "zz-冒烟-双链.md", "content": body})
fid2 = created2["file_id"]
call("/api/files/" + fid2, "PUT", {"content": body})
back2 = call("/api/files/" + target["id"])["file"].get("backlinks") or []
check("反向链接可重复建立", any(b["id"] == fid2 for b in back2), back2)
call("/api/files/" + fid2, "DELETE")

print("\n%s  结论：%s" % ("=" * 8, "全部通过" if ok_all else "有失败项"))
sys.exit(0 if ok_all else 1)
