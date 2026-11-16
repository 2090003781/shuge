# -*- coding: utf-8 -*-
"""转录链路验收（辞玖 2026-10-02 定的那套）：agent 接口接住 → spool → 独立插件组装 → 入库。

  python scripts/smoke_transcript.py

走的是**真路**，一处不打桩：
  ① 模拟 hook 上报：POST /api/bridge/send（kind=tool-call, tool=agent.record.reasonix）
     → 内核按能力名路由到 **agent.reasonix 自己的接口**
  ② 断言 agent 接口把事件**落进 spool**（data/transcripts/<agent>/<日期>.jsonl）
  ③ 断言**没串**：同一事件绝不能落进 agent.dsh 的 spool（两个接口同名能力会随机投错家）
  ④ 让 plugin.transcript 收一天（transcript.flush）→ 断言**库里出现** vault/日志/<agent>-raw/<日期>/会话.md
  ⑤ 断言**不截断**：塞 600 字的 prompt，库里那份必须是完整的 600 字
  ⑥ 再报一条同一天的事件 → 再收一次 → 断言是**更新**（不是又建一篇），两条都在

全部用假日期 2099-01-01，跑完自清（库内那篇 + 那个文件夹 + spool + 暂存 + 状态）。
"""
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(ROOT, "data", "plugins", "token.json")
CORE = "http://127.0.0.1:8790"
LIB = "http://127.0.0.1:8848"
AGENT = "agent.reasonix"
OTHER = "agent.dsh"
DAY = "2099-01-01"
SPOOL = os.path.join(ROOT, "data", "transcripts")
# 笔记名不再写死：现在**一会话一篇**（`会话-<会话短名>.md`），收完之后按实际名字定
LIB_PATH = f"vault/日志/{AGENT}-raw/{DAY}/会话-未标.md"   # 占位（这批假事件没带会话标记）


def notes_in(agent, day):
    """那一天文件夹里有哪些笔记（新的命名是「一会话一篇」）"""
    _st, t = call(LIB + "/api/tree")
    want = f"vault/日志/{agent}-raw/{day}".strip("/")
    out = []

    def walk(node, prefix):
        p = (prefix + "/" + node.get("name", "")).strip("/") if node.get("name") else prefix
        if p == want:
            out.extend([f.get("name") for f in (node.get("files") or [])])
        for c in (node.get("children") or []):
            walk(c, p)
    walk(t.get("tree") or {}, "")
    return sorted(out)


def state_pop_prefix(agent, day):
    """清状态：键现在是 `agent/day/会话`，按前缀删"""
    sf = os.path.join(SPOOL, ".state.json")
    if not os.path.isfile(sf):
        return 0
    try:
        stt = json.load(open(sf, encoding="utf-8"))
        kill = [k for k in stt if k.startswith(f"{agent}/{day}/")]
        for k in kill:
            stt.pop(k, None)
        json.dump(stt, open(sf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        return len(kill)
    except Exception as e:
        print("   状态清理失败（忽略）：", e)
        return 0
def lines_of(p):
    """读一个 jsonl 的行（不存在就回空表）"""
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return [x for x in f if x.strip()]
    except Exception:
        return []


LONG = "这是一段用来验「不截断」的很长的用户消息。" * 30     # 约 600 字
OK, BAD = [], []


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print("  %s %s%s" % ("✅" if cond else "❌", name, ("  | " + str(extra)[:180]) if extra else ""))


def plugin_token():
    """上报面是 strict 鉴权（config.yaml: security.agent_uplink_auth=strict）——
    平台外接入方自己去读这个文件；reasonix hook 的 plugin_token() 也是这么读的。"""
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return str(json.load(f).get("token") or "")
    except Exception:
        return ""


def call(url, body=None, method="GET", timeout=180):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json"}
    tok = plugin_token()
    if tok:
        h["X-Plugin-Token"] = tok
    r = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]


def uplink(agent, tool, ev):
    """走 agent 上报面（外部 agent 用的那条，soft 鉴权）"""
    return call(CORE + "/api/bridge/send",
                {"id": "zz-test-%d" % int.from_bytes(os.urandom(3), "big"),
                 "agentId": agent, "kind": "tool-call",
                 "call": {"tool": tool, "args": {"event": ev}}}, "POST")


def flush(agent, day):
    return call(CORE + "/api/bridge/plugins/call",
                {"plugin": "plugin.transcript", "tool": "transcript.flush",
                 "args": {"agent": agent, "day": day, "force": True}, "timeoutMs": 60000}, "POST")


def tree_find(path):
    """在库里按路径找文件/文件夹 id"""
    _st, t = call(LIB + "/api/tree")
    hit = {"file": None, "folder": None}
    want = path.replace("\\", "/").strip("/")

    def walk(node, prefix):
        p = (prefix + "/" + node.get("name", "")).strip("/") if node.get("name") else prefix
        if p == want:
            hit["folder"] = node.get("id")
        for f in node.get("files", []):
            fp = (p + "/" + f.get("name", "")).strip("/")
            if fp == want:
                hit["file"] = f.get("id")
        for c in node.get("children", []):
            walk(c, p)
    walk(t.get("tree") or {}, "")
    return hit


def spool_path(agent):
    return os.path.join(SPOOL, agent, DAY + ".jsonl")


def content_of(path):
    st, d = call(LIB + "/api/content?path=" + urllib.parse.quote(path))
    if st != 200 or not isinstance(d, dict):
        return st, None
    # 正文挂在 item 里（跟 /api/files/<id> 把正文挂在 file 里一个道理）
    body = d.get("content")
    if body is None and isinstance(d.get("item"), dict):
        body = (d.get("item") or {}).get("content")
    if body is None:
        print("      （/api/content 回的键：%s）" % sorted(d.keys()))
    return st, body


fid = None
try:
    print("① 模拟 hook 上报（走真上报面 → 内核按能力名路由）")
    st, d = uplink(AGENT, "agent.record." + AGENT.split(".")[-1],
                   {"type": "UserPromptSubmit", "time": DAY + "T00:00:01+08:00",
                    "payload": {"prompt": LONG}})
    res = (d or {}).get("result") if isinstance(d, dict) else None
    chk("POST /api/bridge/send 回 200 且工具调用成功", st == 200 and res and res.get("ok"),
        json.dumps(d, ensure_ascii=False)[:220])
    chk("agent 接口回执里说记下了", bool(res and (res.get("data") or {}).get("recorded")),
        (res or {}).get("data"))

    print("\n② agent 接口把事件落进了 spool")
    sp = spool_path(AGENT)
    chk("spool 文件在", os.path.isfile(sp), sp)
    lines = []
    if os.path.isfile(sp):
        lines = [x for x in open(sp, encoding="utf-8") if x.strip()]
    chk("里面就 1 条", len(lines) == 1, len(lines))
    if lines:
        ev = json.loads(lines[0])
        chk("事件原文完整（600 字一个字没少）",
            ev.get("payload", {}).get("prompt") == LONG,
            "存了 %d 字，期望 %d" % (len(ev.get("payload", {}).get("prompt") or ""), len(LONG)))

    print("\n③ 没串到别的 agent 的接口（同名能力会随机投错家）")
    chk("agent.dsh 的 spool 里没有这条", not os.path.isfile(spool_path(OTHER)), spool_path(OTHER))

    print("\n④ 让转录插件收这一天")
    st, d = flush(AGENT, DAY)
    chk("transcript.flush 回 200", st == 200 and (d or {}).get("ok"),
        json.dumps(d, ensure_ascii=False)[:220])
    _rep = (d or {}).get("data") or {}
    chk("它说收下了（created 或 appended；老键 imported/updated 也认）",
        bool(_rep.get("created") or _rep.get("appended")
             or _rep.get("imported") or _rep.get("updated")), _rep)
    names = notes_in(AGENT, DAY)
    if names:
        LIB_PATH = f"vault/日志/{AGENT}-raw/{DAY}/" + names[0]
    chk("那天文件夹里是**一会话一篇**（不再是整天揉成 会话.md）",
        bool(names) and names[0].startswith("会话-"), names)

    print("\n⑤ 库里出现了，而且是完整原文")
    st, body = content_of(LIB_PATH)
    chk("库内路径取得到正文", st == 200 and bool(body), "status=%s path=%s" % (st, LIB_PATH))
    chk("正文里有完整的那 600 字（没被砍）", bool(body) and LONG in body,
        "正文 %d 字" % len(body or ""))
    chk("正文里带事件类型标记", bool(body) and "@UserPromptSubmit" in body)
    hit = tree_find(LIB_PATH)
    fid = hit["file"]
    chk("树里能找到这篇（真进库了，不只是能读）", bool(fid), hit)

    print("\n⑥ 同一天再来一条 → 应该是更新，不是又建一篇")
    st, d2 = uplink(AGENT, "agent.record." + AGENT.split(".")[-1],
                    {"type": "AssistantMessage", "time": DAY + "T00:00:02+08:00",
                     "payload": {"content": "第二条：助手答了一句。"}})
    chk("第二条也记下了", st == 200 and ((d2 or {}).get("result") or {}).get("ok"),
        json.dumps(d2, ensure_ascii=False)[:160])
    st, d = flush(AGENT, DAY)
    chk("再收一次回 200", st == 200 and (d or {}).get("ok"), json.dumps(d, ensure_ascii=False)[:160])
    st2, body2 = content_of(LIB_PATH)
    chk("两条都在同一篇里（同会话**追加**，不是新建一篇）",
        bool(body2) and LONG in body2 and "第二条：助手答了一句。" in body2,
        "正文 %d 字" % len(body2 or ""))
    chk("回执里说是追加（appended）而不是整天重写",
        bool(((d or {}).get("data") or {}).get("appended")),
        ((d or {}).get("data") or {}).get("appended"))
    hit2 = tree_find(LIB_PATH)
    chk("还是同一篇（id 没变）", bool(fid) and hit2["file"] == fid, (fid, hit2["file"]))
finally:
    print("\n⑦ 清场")
    hit = tree_find(LIB_PATH)
    if hit["file"]:
        st, _ = call(LIB + "/api/files/" + hit["file"], None, "DELETE")
        print("   删库内那篇：", st)
    hit = tree_find(f"vault/日志/{AGENT}-raw/{DAY}")
    if hit["folder"]:
        st, _ = call(LIB + "/api/folders/" + hit["folder"], None, "DELETE")
        print("   删那一天的空文件夹：", st)
    hit = tree_find(f"vault/日志/{AGENT}-raw")
    if hit["folder"]:
        st, _ = call(LIB + "/api/folders/" + hit["folder"], None, "DELETE")
        print("   删 agent-raw 这个空文件夹（本来就空才删得掉）：", st)
    for p in (spool_path(AGENT), spool_path(OTHER)):
        if os.path.isfile(p):
            os.remove(p)
            print("   删 spool：", p)
    shutil.rmtree(os.path.join(SPOOL, ".out", AGENT, DAY), ignore_errors=True)
    print("   清状态里的这几条：", state_pop_prefix(AGENT, DAY))
    st, body3 = content_of(LIB_PATH)
    chk("库里那份已经删干净", not body3, "status=%s 正文=%d 字" % (st, len(body3 or "")))

# ── ⑧ 再来一遍：这次走 **agent.dsh**（DSH 插件 dsh-transcript 用的入口是 agent.record.dsh）──
# 为什么要再跑一遍：真正在转录 dsh 会话的是那个 node 插件，它调的是 agent.record.dsh ——
# 只验 agent.reasonix 会漏掉这条真路。
DAY2 = "2099-01-02"
AG2 = "agent.dsh"
LIB_PATH2 = f"vault/日志/{AG2}-raw/{DAY2}/会话.md"
MARK2 = "ZZ-DSH-TRANSCRIPT"
try:
    print("\n⑧ 同一套链，换成 agent.dsh（DSH 插件那条路）")
    st, d = uplink(AG2, "agent.record." + AG2.split(".")[-1],
                   {"type": "AssistantMessage", "time": DAY2 + "T00:00:01+08:00",
                    "payload": {"content": MARK2 + " 这是 dsh 侧的原文。" * 12}})
    res = (d or {}).get("result") if isinstance(d, dict) else None
    chk("agent.dsh 的上报也成功", st == 200 and res and res.get("ok"),
        json.dumps(d, ensure_ascii=False)[:180])
    sp2 = os.path.join(SPOOL, AG2, DAY2 + ".jsonl")
    chk("落进了 agent.dsh 自己的 spool", os.path.isfile(sp2), sp2)
    chk("没串到 agent.reasonix",
        not any(MARK2 in x for x in lines_of(spool_path(AGENT))), "reasonix spool")
    st, d = flush(AG2, DAY2)
    chk("转录插件收得动 agent.dsh", st == 200 and (d or {}).get("ok"),
        json.dumps(d, ensure_ascii=False)[:180])
    names2 = notes_in(AG2, DAY2)
    if names2:
        LIB_PATH2 = f"vault/日志/{AG2}-raw/{DAY2}/" + names2[0]
    st, body = content_of(LIB_PATH2)
    chk("库里出现 agent.dsh 的那一篇", bool(body), "status=%s" % st)
    chk("内容是完整原文（没被砍）", bool(body) and MARK2 in body,
        "正文 %d 字" % len(body or ""))
    chk("树里找得到", bool(tree_find(LIB_PATH2)["file"]))
finally:
    print("  清 agent.dsh 那天的场")
    h = tree_find(LIB_PATH2)
    if h["file"]:
        call(LIB + "/api/files/" + h["file"], None, "DELETE")
    for p in (f"vault/日志/{AG2}-raw/{DAY2}", f"vault/日志/{AG2}-raw"):
        hh = tree_find(p)
        if hh["folder"]:
            call(LIB + "/api/folders/" + hh["folder"], None, "DELETE")
    for p in (os.path.join(SPOOL, AG2, DAY2 + ".jsonl"),):
        if os.path.isfile(p):
            os.remove(p)
    shutil.rmtree(os.path.join(SPOOL, ".out", AG2, DAY2), ignore_errors=True)
    state_pop_prefix(AG2, DAY2)
    st, b3 = content_of(LIB_PATH2)
    chk("agent.dsh 那份也删干净", not b3, "正文=%d 字" % len(b3 or ""))

print("\n通过 %d / %d；失败：%s" % (len(OK), len(OK) + len(BAD), "、".join(BAD) if BAD else "无"))
sys.exit(1 if BAD else 0)
