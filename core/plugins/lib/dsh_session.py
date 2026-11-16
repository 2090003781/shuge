# -*- coding: utf-8 -*-
"""DSH 会话存储的读取件（给内核侧「拉」的那条转录链路用）。

**为什么是「拉」而不是「推」**（辞玖 2026-10-04 追问"怎么会一死没有消息"）：
推的那一半住在 **DSH 进程里**（`~/.dsh/profiles/*/plugins/dsh-transcript`）——
内核监督不到它，它一死就是**静默**的。2026-10-03 12:30 真发生了：outbox 里只剩一条
测试事件，之后一个字节都没有，`.state.json` 还在每 30 秒报 `ok:true`：**"我跑得好好的" ≠ "我收到了东西"**。

而 DSH 自己把会话**完整**写在盘上：
    ~/.dsh/sessions/<cwd 编码>/session-<uuid>/session.v3.jsonl.zstd
一条记录一个 zstd 帧、只追加、每条带**自增唯一 `seq`**（实测 49117 条 0..49115，无重复）。
所以内核侧只要按「字节偏移 + seq 去重」读它，就能拿到完整会话 —— 而且**这个读进程由内核监督**
（写在 config.yaml 的 plugins 里，Supervisor 管崩溃重启），出问题还能发告警。

格式要点（2026-10-04 实测）：**多个 zstd 帧直接拼接**（文件头就是魔数 28 b5 2f fd；
40MB 的文件里 23843 个魔数 = 一条记录一帧）→ 必须 `read_across_frames=True`，逐帧一路读下去。
"""
import datetime
import glob
import json
import os

SESSION_SUFFIX = "session.v3.jsonl.zstd"
DEFAULT_SESSIONS_DIR = os.path.join(os.path.expanduser("~"), ".dsh", "sessions")

# 哪些记录进转录（其余是过程噪声：step/start·step/end 各 7000+ 条、tool/call 与
# assistant/message 里的 tool-call 分片重复、permission/sandbox 之类的状态行）
KEEP_TYPES = {
    "session": "会话",
    "turn/start": "轮次",
    "turn/end": "轮次",
    "user/message": "用户",
    "assistant/message": "助手",
    "tool/result": "工具结果",
    "compaction/summary": "上下文压缩",
    # `system/message` 不在列：它是**系统提示词**（正文在 data.message.content，一份几千字），
    # 不是对话，每次会话还重复好几遍 —— 原始会话文件里有全份，转录不必抄一遍（2026-10-04 定）
}

# user/message 里只有 `source.kind == "user"` 是真人在说话；
# 其余（plugin 7914 条 = 工具回灌、goal、agent-message…）要么与 tool/result 重复，要么是内部管道
USER_SOURCE_KEEP = {"user"}


def _clip(text, limit, what=""):
    """超长就截断，但**一定留痕**（辞玖的规矩：不许悄悄砍内容）"""
    t = text or ""
    if limit and len(t) > limit:
        return t[:limit] + "\n\n…（%s共 %d 字，这里留了前 %d 字）" % (what or "内容", len(t), limit)
    return t


def rec_ms(rec):
    """一条记录的时间（毫秒）。**`session` 头没有 `time`，它用 `createdAt`。**

    2026-10-04 栽过：没这一层，`iso_of(None)` 退回"此刻" → 66 个历史会话的"会话开始"
    全算成今天，`--since` 也拦不住（今天那份转录开头堆了一串重复的"（会话开始）"）。
    """
    if not isinstance(rec, dict):
        return None
    return rec.get("time") or rec.get("createdAt")


def _ms_to_local(ms):
    try:
        return datetime.datetime.fromtimestamp(float(ms) / 1000.0)
    except Exception:
        return None


def iso_of(ms):
    """DSH 的毫秒时间戳 → 本地 ISO（带时区偏移，库里按它归天）"""
    dt = _ms_to_local(ms)
    if dt is None:
        return datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    return dt.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def day_of(ms):
    dt = _ms_to_local(ms)
    return (dt or datetime.datetime.now()).strftime("%Y-%m-%d")


def find_sessions(root=None):
    """扫出所有会话文件 → [{id, path, cwd, size, mtime}]（id 就是 session-<uuid> 那层目录名）"""
    root = root or DEFAULT_SESSIONS_DIR
    out = []
    for p in glob.glob(os.path.join(root, "**", SESSION_SUFFIX), recursive=True):
        try:
            st = os.stat(p)
        except OSError:
            continue
        sid = os.path.basename(os.path.dirname(p))
        cwd = os.path.basename(os.path.dirname(os.path.dirname(p)))
        out.append({"id": sid, "path": p, "cwd": cwd, "size": st.st_size, "mtime": st.st_mtime})
    return sorted(out, key=lambda x: x["mtime"])


def decode_from(path, offset=0):
    """从字节偏移读到底 → ([记录...], offset_after, error)。

    能一路读下去靠 `read_across_frames=True`（多帧拼接）。偏移落在帧中间会报错 ——
    调用方据此**从 0 重读**（成本可接受，且有 `seq` 去重兜底，不会重复入库）。
    """
    import zstandard as zstd
    recs = []
    try:
        with open(path, "rb") as f:
            if offset:
                f.seek(offset)
            with zstd.ZstdDecompressor().stream_reader(f, read_across_frames=True) as r:
                buf = r.read()
    except Exception as e:
        return [], offset, "%s: %s" % (type(e).__name__, e)
    for line in buf.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except Exception:
            continue
    try:
        after = os.path.getsize(path)
    except OSError:
        after = offset
    return recs, after, ""


def _join_text(parts, with_reasoning=False):
    """内容分片 → 正文（text 全要；reasoning 默认不要，要了单独标出来）"""
    texts, thinks = [], []
    for c in parts or []:
        if not isinstance(c, dict):
            continue
        t = c.get("type")
        if t == "text" and c.get("text"):
            texts.append(str(c["text"]))
        elif t == "reasoning" and with_reasoning and c.get("text"):
            thinks.append(str(c["text"]))
        elif t == "image":
            texts.append("（一张图）")
    body = "\n\n".join(texts).strip()
    if thinks:
        body = (body + "\n\n" if body else "") + "<details><summary>思考</summary>\n\n" \
               + "\n\n".join(thinks).strip() + "\n\n</details>"
    return body


def _tool_calls(parts, args_max=2000):
    out = []
    for c in parts or []:
        if isinstance(c, dict) and c.get("type") == "tool-call":
            name = str(c.get("name") or "?")
            out.append("工具 `%s`\n\n参数：\n\n%s" % (name, _clip(str(c.get("arguments") or ""),
                                                              args_max, "参数")))
    return "\n\n".join(out)


def to_event(rec, session_id="", cwd="", with_reasoning=False,
             tool_result_max=8000, tool_args_max=2000):
    """一条 DSH 记录 → spool 事件（与 `agent_channel.record()` 同形）；不该进转录的回 None。

    payload 走 DSH 族形状 `{session, raw: {...}}` —— `plugin.transcript._content_of` 认这一族：
    raw 里有 `content` / `prompt` / `toolResult` / `toolName` 就能原样渲染。
    """
    t = str(rec.get("type") or "")
    if t not in KEEP_TYPES:
        return None
    if rec_ms(rec) is None:
        # 没有时间字段就**不猜时间**：猜了会把它算成"此刻"，历史记录就会混进今天那份转录
        return None
    d = rec.get("data") or {}
    raw = {"session_id": session_id, "cwd": cwd}
    typ = t

    if t == "user/message":
        sk = str(((d.get("source") or {}).get("kind")) or "")
        if sk not in USER_SOURCE_KEEP:
            return None
        raw["prompt"] = _join_text(d.get("content"), with_reasoning)
    elif t == "assistant/message":
        parts = ((d.get("message") or {}).get("content")) or []
        body = _join_text(parts, with_reasoning)
        tc = _tool_calls(parts, tool_args_max)
        raw["content"] = (body + "\n\n" + tc).strip() if tc else body
        raw["turn"], raw["step"] = d.get("turn"), d.get("step")
    elif t == "tool/result":
        m = d.get("message") or {}
        name = str(((m.get("source") or {}).get("callId")) or "")
        call = ""
        for c in (m.get("content") or []):
            if isinstance(c, dict) and c.get("type") == "tool-result":
                call = str(c.get("toolCallId") or call)
                txt = "".join(str(x.get("text") or "") for x in (c.get("content") or [])
                              if isinstance(x, dict))
                raw["toolResult"] = _clip(txt, tool_result_max, "工具结果")
                raw["toolName"] = call or name
        if "toolResult" not in raw:
            return None
    elif t == "turn/start":
        raw["content"] = "（第 %s 轮开始）" % d.get("turn")
    elif t == "turn/end":
        r = d.get("reason") or {}
        raw["content"] = "（第 %s 轮结束：%s）" % (d.get("turn"), r.get("kind") or "")
    elif t == "session":
        raw["content"] = ("（会话开始）cwd=%s · 预设=%s · DSH 会话格式 v%s"
                          % (rec.get("cwd") or cwd, rec.get("agentPreset") or "?", rec.get("version")))
        raw["session_id"] = rec.get("id") or session_id
    elif t == "compaction/summary":
        raw["content"] = "（上下文被压缩，摘要如下）\n\n" + _join_text(
            (d.get("message") or {}).get("content") or d.get("content"), with_reasoning)

    return {
        "type": typ,
        "source": "dsh",
        "target": "agent.dsh",
        "agent": "agent.dsh",
        "time": iso_of(rec_ms(rec)),
        "seq": rec.get("seq"),
        "payload": {"session": session_id, "raw": raw},
    }
