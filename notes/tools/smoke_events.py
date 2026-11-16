"""事件协议的端到端冒烟（EVENT v0.2）。

跑法：python tools/smoke_events.py [base]
- 走真实 HTTP：新建/保存/删除/重建 → 检查事件是否按协议落下来
- 大载荷外置（ref）在临时库上单独验，不污染真库的事件日志
"""
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
ok_all = True
sys.path.insert(0, "src")


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


def events_after(gseq, types=""):
    # 全量重建会为每个文件发一条 chunk.indexed（1000+ 条），
    # 想看动作事件必须按类型过滤，否则位点窗里全是索引事件
    q = "/api/events?after=%d&limit=1000" % gseq
    if types:
        q += "&types=" + types
    return call(q)[1]["events"]


def find(evs, type_):
    return [e for e in evs if e["type"] == type_]


print("① 事件接口")
st, r = call("/api/events?limit=5")
check("可读", st == 200 and r.get("ok"), r.get("error"))
base_gseq = r["last_gseq"]
check("给出位点 last_gseq", isinstance(base_gseq, int), base_gseq)

print("② 新建 / 保存 / 删除 一条笔记")
content = "# 事件冒烟\n\n第一版内容。\n"
st, made = call("/api/files", "POST", {"name": "zz-冒烟-事件.md", "content": content})
fid = made["file_id"]
call("/api/files/" + fid, "PUT", {"content": content + "第二版加了一句。\n"})
call("/api/files/" + fid, "DELETE")

evs = events_after(base_gseq)
created = find(evs, "note.created")
updated = find(evs, "note.updated")
deleted = find(evs, "note.deleted")
check("note.created 发出", bool(created))
check("payload 字段齐", created and all(k in created[0]["payload"] for k in
      ("file_id", "folder_id", "name", "kind", "size")), created[0]["payload"] if created else None)
check("note.updated 发出", bool(updated))
check("带 content_hash 与 changed_chars", updated and updated[0]["payload"].get("content_hash")
      and updated[0]["payload"].get("changed_chars", 0) > 0, updated[0]["payload"] if updated else None)
check("note.deleted 发出", bool(deleted))

print("③ 协议字段")
for e in evs[:8]:
    if not e["id"].startswith("evt-") or not e["v"]:
        break
check("id 形如 evt-<source>-<seq>", all(e["id"].startswith("evt-%s-" % e["source"]) for e in evs))
check("v=1", all(e["v"] == 1 for e in evs))
check("time 带时区偏移", all(("+" in e["time"][10:]) or e["time"].endswith("Z") for e in evs),
      evs[0]["time"] if evs else None)
check("gseq 全局递增", all(evs[i]["gseq"] < evs[i + 1]["gseq"] for i in range(len(evs) - 1)))
per_src = {}
for e in evs:
    per_src.setdefault(e["source"], []).append(e["seq"])
check("seq 源内递增", all(v == sorted(v) and len(set(v)) == len(v) for v in per_src.values()),
      {k: v for k, v in per_src.items()})

print("④ 动作配对")
st, r = call("/api/reindex", "POST", {"reason": "smoke"})
evs2 = events_after(base_gseq, types="platform.action.started,platform.action.settled")
started = find(evs2, "platform.action.started")
settled = find(evs2, "platform.action.settled")
check("发了 started", bool(started))
check("发了 settled", bool(settled))
check("同一动作 id 配对", started and settled
      and started[-1]["payload"]["action"]["id"] == settled[-1]["payload"]["action"]["id"],
      (started[-1]["payload"]["action"], settled[-1]["payload"]["action"]) if started and settled else None)
check("settled 带 duration/status", settled and settled[-1]["payload"]["outcome"]["status"] == "ok"
      and settled[-1]["payload"]["outcome"]["duration_ms"] >= 0,
      settled[-1]["payload"]["outcome"] if settled else None)
check("chunk.indexed 跟着发出", bool(find(events_after(base_gseq, types="chunk.indexed"), "chunk.indexed")),
      "全量重建每个文件一条")

print("⑤ 回放（dry_run）")
st, r = call("/api/events/replay?from=%d&dry_run=1" % base_gseq)
check("干跑返回区间内事件", st == 200 and r.get("events", 0) >= 3, r)
check("列出涉及的文件", isinstance(r.get("files"), list))
st, r2 = call("/api/events/replay?from=%d&to=%d&dry_run=0" % (base_gseq, base_gseq + 1000))
check("真跑会重建派生层", st == 200 and r2.get("dry_run") is False, r2.get("reindexed"))

print("⑥ 大载荷走 ref（临时库，不污染真库）")
tmp = tempfile.mkdtemp(prefix="ai-notes-events-")
try:
    from db import Database
    from events import EventLog
    tdb = Database(tmp)
    log = EventLog(tdb, source="smoke")
    big = {"text": "x" * 9000}
    ev = log.emit("note.updated", big)
    check("payload 被摘要化", ev["payload"].get("_ref") is True, ev["payload"])
    check("ref 指向 blob", bool(ev["ref"] and ev["ref"]["kind"] == "file"), ev["ref"])
    blob = os.path.join(tmp, ev["ref"]["path"].replace("/", os.sep))
    check("blob 真的写出来了", os.path.isfile(blob) and os.path.getsize(blob) > 9000,
          os.path.getsize(blob) if os.path.isfile(blob) else None)
    back = log.read(after=0, limit=1)[0]
    check("读回来 ref 还在", back["ref"] and back["ref"]["bytes"] > 9000, back["ref"])
    tdb.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n========  结论：%s" % ("全部通过" if ok_all else "有失败项"))
sys.exit(0 if ok_all else 1)
