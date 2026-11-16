"""验收：agent 通过平台接口调库，库那边记下的写入者是 `agent:<插件>`（辞玖 2026-09-24 定的口径）。

  python scripts/acc_writer_agent.py

走的是真链路：POST /api/ops/call（8795，agent 接口）→ 平台代调库（8848）→ 库把 X-Writer 记进 events.writer。
验：建一篇 → 库的 writers 里出现 agent:dsh；改一篇 → 还是 agent:dsh；删掉 → 收尾干净。
"""

import os as _os, sys as _sys          # 门禁用（2026-09-30 体检）

if "--yes" not in _sys.argv and _os.environ.get("COREHOST_ALLOW_DESTRUCTIVE") != "1":
    print(__doc__ or "")
    print("\n【门禁】%s 会真的动到**运行中的枢阁 / 库文件夹**（这个脚本忽略参数，--help 也照跑）。"
          % _os.path.basename(__file__))
    print("       确认要跑：python %s --yes（或设 COREHOST_ALLOW_DESTRUCTIVE=1）"
          % _os.path.basename(__file__))
    _sys.exit(2)
import json
import sys
import time
import urllib.error
import urllib.request

CORE = "http://127.0.0.1:8795"
LIB = "http://127.0.0.1:8848"
OK = True


def call(base, path, body=None, method="GET", timeout=300):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:150] if extra else ""))


def op(plugin, opname, args):
    return call(CORE, "/api/ops/call", {"plugin": plugin, "op": opname, "args": args}, "POST")


st, tree = call(LIB, "/api/tree")
root = (tree.get("tree") or {}).get("id")
stamp = time.strftime("%H%M%S")
name = "zz-写入者验收-%s.md" % stamp
fid = None
try:
    print("① agent 建一篇（经平台 ops → 库）")
    st, d = op("plugin.dsh", "brain.create",
               {"folder_id": root, "name": name, "content": "# 写入者验收\n\n第一版\n"})
    fid = (d.get("result") or {}).get("file_id") or d.get("file_id")
    check("建成功", st == 200 and bool(fid), d if not fid else fid)
    if not fid:
        raise SystemExit(1)

    print("② agent 改一篇")
    st, d = op("plugin.dsh", "brain.update", {"id": fid, "content": "# 写入者验收\n\n第二版\n"})
    check("改成功", st == 200 and d.get("ok") is not False, d.get("error") or "")

    print("③ 库那边记的写入者")
    time.sleep(0.4)
    st, w = call(LIB, "/api/files/%s/writers" % fid)
    ws = (w or {}).get("writers") or []
    tags = {x.get("writer") for x in ws}
    print("     台账：", [(x.get("writer"), x.get("type")) for x in ws[:5]])
    check("有 agent:dsh", "agent:dsh" in tags, tags)

    print("④ 按写入者过滤事件")
    st, head = call(LIB, "/api/events?limit=1")
    after = max(0, int(head.get("last_gseq") or 0) - 300)
    st, d = call(LIB, "/api/events?after=%d&limit=300&writer=agent:*" % after)
    evs = d.get("events") or []
    check("writer=agent:* 能过滤出事件", bool(evs), len(evs))
    check("过滤出来的都是 agent 开头",
          all((e.get("writer") or "").startswith("agent:") for e in evs), evs[:2])

    print("⑤ 收尾")
    # 删除需要 trust 档，plugin.dsh 现在只到 write —— 收尾走库接口（照样带写入者标签）
    st, d = call(LIB, "/api/files/%s" % fid, None, "DELETE", 300) if False else (None, None)
    req = urllib.request.Request(LIB + "/api/files/" + fid, method="DELETE",
                                headers={"X-Writer": "tool:acc"})
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.load(r)
    check("删成功（库接口）", bool(d.get("deleted")), d.get("error") or d.get("deleted"))
    fid = None
finally:
    if fid:
        req = urllib.request.Request(LIB + "/api/files/" + fid, method="DELETE",
                                    headers={"X-Writer": "tool:acc"})
        try:
            urllib.request.urlopen(req, timeout=300).read()
        except Exception:
            pass

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
