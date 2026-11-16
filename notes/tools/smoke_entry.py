"""单入口端到端（经枢阁 8790 的 /notes 代理走一遍真实链路）。

只读 + 自建自清：造的临时笔记最后会删掉。
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790").rstrip("/")
ok_all = True


def call(path, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:(150)] if extra else ""))


print("① 入口页")
st, html = call("/")
check("枢阁外壳/控制台可开", st == 200 and "枢阁" in html, st)
st, shell = call("/console")
check("控制台可开", st == 200 and "控制台" in shell, st)
st, lib = call("/notes/")
check("库页面可开", st == 200 and "枢阁 · 库" in lib, st)
st, _ = call("/notes")
check("/notes 无尾斜杠会跳转", st == 200, st)

print("② 代理白名单与越权")
st, _ = call("/notes/../etc/passwd")
check("路径穿越被挡", st == 404, st)
st, _ = call("/notes/secret.txt")
check("白名单外的路径 404", st == 404, st)
st, d = call("/notes/api/health")
check("库健康接口可用", st == 200 and d.get("ok"), st)
files_before = d["stats"]["files"]
chunks_before = d["stats"]["chunks"]
check("库规模合理", files_before > 1000 and chunks_before > 10000,
      "%d 文件 / %d 块" % (files_before, chunks_before))

print("③ 通过代理做一轮真实读写")
st, tree = call("/notes/api/tree")
check("树可读且有 vault", any(c["name"] == "vault" for c in tree["tree"]["children"]),
      [c["name"] for c in tree["tree"]["children"]])
st, made = call("/notes/api/files", "POST",
                {"name": "zz-体检-端到端.md", "content": "# 体检\n\n目标 @FORMAT-数据格式规范-v0.4 ，另见 **粗体**。\n"})
fid = made.get("file_id")
check("新建成功", st == 201 and fid, made)
st, saved = call("/notes/api/files/" + fid, "PUT",
                 {"content": "# 体检\n\n目标 @FORMAT-数据格式规范-v0.4 与 @EVENT-事件协议-v0.2 。\n"})
check("保存成功并建了 2 条提及", saved.get("saved", {}).get("mentions") == 2, saved.get("saved"))
st, f = call("/notes/api/files/" + fid)
check("读回带 mentions/backlinks/tags", isinstance(f["file"].get("mentions"), list)
      and isinstance(f["file"].get("backlinks"), list) and isinstance(f["file"].get("tags"), list))
st, r = call("/notes/api/search?q=%s&limit=3" % urllib.parse.quote("体检 端到端"))
check("检索接口可用", st == 200 and "hits" in r, st)
check("检索带排除标签说明", "excluded_tags" in r, r.get("excluded_tags"))

print("④ 锁 / 标签 / 事件")
st, lock = call("/notes/api/lock")
check("锁状态可读", st == 200 and isinstance(lock.get("configured"), bool)
      and isinstance(lock.get("unlocked"), bool), lock)
st, tg = call("/notes/api/tags")
check("标签统计可读（logs 在位）", st == 200 and tg["tags"].get("logs", 0) > 0, tg["tags"])
st, ev = call("/notes/api/events?limit=3")
check("事件流可读", st == 200 and ev["last_gseq"] > 0, ev["last_gseq"])
types = [e["type"] for e in ev["events"]]
check("最近事件里有写入事件", any(t.startswith(("note.", "chunk.")) for t in types), types)

print("⑤ 清场")
# 这一步走的是宿主代理，库忙的时候代理会返 502/504（不是删失败）—— 重试两次（2026-09-26）
st = 0
for _try in range(3):
    st, _ = call("/notes/api/files/" + fid, "DELETE")
    if st in (200, 404):
        break
    time.sleep(1.0)
check("临时笔记已删除", st in (200, 404), st)
d2 = {}
for _try in range(3):
    st, d2 = call("/notes/api/health")
    if st == 200 and "stats" in d2:
        break
    time.sleep(1.0)
check("库规模复原", d2["stats"]["files"] == files_before,
      "%s vs %s" % (d2["stats"]["files"], files_before))

print("\n========  结论：%s" % ("全部通过" if ok_all else "有失败项"))
sys.exit(0 if ok_all else 1)
