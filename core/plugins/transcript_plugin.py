#!/usr/bin/env python3
"""转录插件（独立进程插件）—— 把各 agent 的日志记录组装成"一天一份"写进库。

辞玖 2026-10-02 定案。分工（这条缝是整个设计的命门）：

    hook/agent ──上报──▶ **agent 接口**（plugin.reasonix / plugin.dsh）
                            · 只做「接住」：追加到 `data/transcripts/<agent>/<日期>.jsonl`，立刻回执
                            · 顺手唤醒本插件（发出去就不管）
                                        │
                                        ▼
                            **本插件**：组装 → 写库 → 盘上投影由库自己落

为什么这么切：「接住」不能依赖下游（hook 只有 5 秒预算，它的注释里写着"端口不通最多等 5.5s →
超时 warn/block"）；「入库」才可以慢、可以重试、可以重跑。spool 在盘上，所以本插件挂/重启/
想改格式重算历史，**都不丢**。

与归档的投递箱（plugin.inbox）的关系：那套"事件驱动 + 兜底对账"的教训照抄，但**不做双向镜像** ——
那边要同时管 vault⇄库文件夹（已随"库是真相源"退役）；这里只做一件事：**agent 日志 → 库**。

**不截断**：组装时用事件原文，一个字节都不砍（旧程序 writer.go 把助手正文砍到 100 字节、prompt 砍到
150 字节且不留痕 —— 那条链退役后本插件是唯一入口，所以从这儿起内容是完整的）。
"""
import argparse
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))

from detail_ledger import DetailLedger  # noqa: E402
from probe import Probe, auth_headers  # noqa: E402

CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:8790")
CORE_DIR = os.environ.get("CORE_DIR", os.path.dirname(HERE))
LIB_URL = (os.environ.get("AINOTES_URL") or "http://127.0.0.1:8848").rstrip("/")

PLUGIN_ID = "plugin.transcript"
SPOOL_DIR = os.path.join(CORE_DIR, "data", "transcripts")
STAGING_DIR = os.path.join(SPOOL_DIR, ".out")
STATE_FILE = os.path.join(SPOOL_DIR, ".state.json")
# 笔记名现在按会话生成：`会话-<会话短名>.md`（一会话一篇；跨天才新建）

# 库内落在哪儿：vault/日志/<agent>-raw/<日期>/会话.md（跟历史的 reasonix-raw 同一处）
LIB_ROOT = "vault/日志"

running = True
_lock = threading.Lock()
_stats = {"runs": 0, "assembled": 0, "imported": 0, "appended": 0, "updated": 0, "skipped": 0,
          "failed": 0, "wakes": 0, "bytes_out": 0, "events_in": 0, "errors": 0,
          "last_run": "", "last_error": "", "agents": []}
_DETAIL = None
_wake = {"dirty": False, "due": 0.0}      # 被唤醒就早点跑
# **合并写入**的去抖：一串事件（一次交付一批）合成一次整天重写。
# 这不是「等静默」—— 它不影响「有就录」，只是不让同一秒里重写十遍（2026-10-04 辞玖定的「有就录」）
_DEBOUNCE = 2.0


def log(msg):
    print(f"[transcript] {msg}", flush=True)


def _pid_alive(pid):
    """跨平台判进程存活（与 log_plugin 同款：要看退出码，不能只看句柄非空）"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259      # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def handle_stop(signum, frame):
    global running
    log(f"收到信号 {signum}，优雅退出")
    running = False


def detail_ledger():
    global _DETAIL
    if _DETAIL is None:
        try:
            _DETAIL = DetailLedger(PLUGIN_ID)
        except Exception as e:
            log(f"分账不可用（忽略）: {e}")
            _DETAIL = False
    return _DETAIL or None


def lib_call(path, body=None, method="GET", timeout=180):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(LIB_URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "X-Writer": "transcript"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"状态写盘失败（忽略）: {e}")


# ── 事件 → Markdown ──────────────────────────────────────────
_TOOL_OF = re.compile(r'"name"\s*:\s*"([A-Za-z0-9_.\-]+)"')


_LABELS = {"SessionStart": "（会话开始）", "SessionEnd": "（会话结束）",
           "TurnStart": "", "Stop": "", "StepStart": ""}


def _as_text(v):
    """值 → 文本（字符串原样；结构体转 JSON；都不行给空）"""
    if v in (None, "", [], {}):
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    try:
        return json.dumps(v, ensure_ascii=False, indent=2)
    except Exception:
        return str(v)


def _content_of(ev):
    """把事件里的"内容"抠出来（原样，不截断）。

    事件形状有两族，都要认：
      · **DSH 族**（`dsh-transcript` 插件）：`payload = {session, raw: <原事件>}`，
        真正的文本在 `raw.prompt` / `raw.content` / `raw.toolName`+`raw.toolArgs` / `raw.toolResult`
      · **reasonix 族**（`hook_logger.py`）：`payload = {session, level, log_type, content, raw: <stdin payload>}`，
        文本多半在 `payload.content`，原文在 `payload.raw`
    顶层找不到就钻一层进 `raw`；再找不到才退回 JSON（并明确标注"整条原文"）。
    """
    p = ev.get("payload") or {}
    if not isinstance(p, dict):
        return _as_text(p)
    layers = [p]
    if isinstance(p.get("raw"), dict):
        layers.append(p["raw"])          # 钻一层

    for L in layers:
        tool = str(L.get("toolName") or L.get("tool_name") or L.get("tool") or "")
        for k in ("content", "prompt", "text", "message", "toolResult", "tool_response",
                  "tool_input", "result"):
            t = _as_text(L.get(k))
            if t:
                if tool and k in ("toolResult", "tool_response", "result"):
                    return "工具 `%s` 的结果：\n\n%s" % (tool, t)
                return t
        args = L.get("toolArgs") or L.get("tool_input") or L.get("arguments")
        if tool or args:
            head = ("工具 `%s`" % tool) if tool else "工具调用"
            a = _as_text(args)
            return ("%s\n\n参数：\n\n%s" % (head, a)) if a else head

    label = _LABELS.get(str(ev.get("type") or ""), "")
    if label:
        return label
    return "（无正文）\n\n整条原文：\n\n" + _as_text(p)


def _tool_of(ev):
    p = ev.get("payload") or {}
    if isinstance(p, dict):
        return str(p.get("tool_name") or p.get("tool") or "").strip()
    return ""


def render_head(agent, day, label, session):
    """一篇会话笔记的篇头（**只在新建时写一次**）。

    故意**不写"共 N 条事件"**：这篇是会一直追加的，写死的条数一会儿就骗人。
    """
    return (f"# {agent} · {day} · {label}\n\n"
            f"> 由 `{PLUGIN_ID}` 从 `data/transcripts/{agent}/{day}.jsonl` **按会话**组装；"
            f"同一会话后续事件**直接追加在文末**（跨天才新建一篇）。**内容是原文，未截断。**\n>\n"
            f"> 会话 id：`{session or '（这批事件没带会话标记）'}`\n\n")


def render_events(events):
    """事件 → Markdown 片段（**不截断**；长得离谱的单条也不砍，库那边自己分块）"""
    out = []
    for ev in events:
        t = str(ev.get("time") or "")
        hhmm = t[11:19] if len(t) >= 19 else t
        typ = str(ev.get("type") or "?")
        tool = _tool_of(ev)
        out.append(f"## {hhmm} · @{typ}" + (f" · {tool}" if tool else ""))
        out.append("")
        body = _content_of(ev)
        out.append(body if body.strip() else "（空）")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def group_by_session(events):
    """按 `payload.session` 把一天的事件分会话（保持首次出现顺序）。

    同一个会话的事件在 spool 里是**交错**的（多会话同时在写），所以必须分组，
    否则一篇笔记里就混着好几场对话 —— 这也正是"整天一份"难看的根子。
    """
    order, groups = [], {}
    for ev in events:
        p = ev.get("payload")
        sess = str((p.get("session") if isinstance(p, dict) else "") or "")
        if sess not in groups:
            groups[sess] = []
            order.append(sess)
        groups[sess].append(ev)
    return [(s, groups[s]) for s in order]


def session_label(sess):
    """会话 id → 笔记名片段（安全、可读）"""
    import re as _re
    raw = _re.sub(r"[^A-Za-z0-9._-]", "_", str(sess or "")).strip("_")
    if not raw:
        return "会话-未标"
    for pre in ("dsh-session-", "session-", "dsh-"):
        if raw.startswith(pre):
            raw = raw[len(pre):]
            break
    parts = [x for x in raw.split("-") if x]
    # 优先挑"像 id"的那一段（全十六进制、6 字以上）——reasonix 的会话 id 是
    # `reasonix-20261003112808-afbba1ed`，取第一段会撞成 `会话-reasonix` / `会话-reasonix-2`
    import re as _re2
    pick = next((x for x in parts if len(x) >= 6 and _re2.fullmatch(r"[0-9a-fA-F]+", x)), "")
    if not pick:
        pick = parts[0] if parts else ""
    return "会话-" + (pick[:8] or "未标")


def label_for(st, agent, day, sess):
    """同一天里给会话分配短名；撞名就加序号（状态里记着，跨轮次稳定）"""
    m = st.setdefault("_labels", {}).setdefault(f"{agent}/{day}", {})
    if sess in m:
        return m[sess]
    base, used, name, i = session_label(sess), set(m.values()), session_label(sess), 1
    while name in used:
        i += 1
        name = "%s-%d" % (base, i)
    m[sess] = name
    return name


# ── 组装 + 入库 ──────────────────────────────────────────────
def known_agents():
    """要处理哪些 agent：**本地 spool 目录**为准（平台问不到也不影响干活）。

    为什么不只信平台的 agent 列表：平台重启/内核不在时，spool 里已经有数据了，
    这时候更应该能干活，而不是干等。
    """
    out = []
    try:
        for name in sorted(os.listdir(SPOOL_DIR)):
            p = os.path.join(SPOOL_DIR, name)
            if os.path.isdir(p) and not name.startswith("."):
                out.append(name)
    except Exception:
        pass
    return out


def read_day(agent, day):
    path = os.path.join(SPOOL_DIR, agent, day + ".jsonl")
    evs = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evs.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return [], 0
    try:
        size = os.path.getsize(path)
    except Exception:
        size = 0
    return evs, size


def file_id_by_path(path):
    """库内路径 → file_id（导入回执没给 id 时按路径问回来）"""
    try:
        import urllib.parse as _up
        d = lib_call("/api/content?path=" + _up.quote(path), timeout=60)
        return str(((d.get("item") or {}).get("id")) or "")
    except Exception:
        return ""


def process(force=False, only_agent=None, only_day=None, quiet_seconds=None):
    """扫一遍 spool，把**有新东西的**会话组装进库（**一会话一篇；同会话只追加**）。

    **有就录**（辞玖 2026-10-04）：不再等「安静 N 秒」——
    spool 的职责只是「事件无论多少先一个不落地收住」，收住之后就该往库里录。
    新建只在**第一次见到这个会话**时发生；之后每轮只把**新增的那几条**追加到文末
    （库那边走 `/append`，盘上也按字节追加），**不再整篇重写**。
    `quiet_seconds` 只为兼容旧调用留着，已无作用。
    """
    st = load_state()
    rep = {"agents": [], "created": [], "appended": [], "updated": [], "skipped": [], "failed": []}
    _stats["runs"] += 1
    _stats["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    for agent in known_agents():
        if only_agent and agent != only_agent:
            continue
        adir = os.path.join(SPOOL_DIR, agent)
        try:
            days = sorted(x[:-6] for x in os.listdir(adir) if x.endswith(".jsonl"))
        except Exception:
            continue
        rep["agents"].append(agent)
        for day in days:
            if only_day and day != only_day:
                continue
            evs, size = read_day(agent, day)
            if not evs:
                continue
            for sess, sevs in group_by_session(evs):
                key = f"{agent}/{day}/{sess or '(无会话标记)'}"
                prev = st.get(key) or {}
                done = int(prev.get("events") or 0)
                if done >= len(sevs) and prev.get("ok") and not force:
                    rep["skipped"].append(f"{key}（没变化）")
                    _stats["skipped"] += 1
                    continue
                new_evs = sevs[done:]
                if not new_evs:
                    rep["skipped"].append(f"{key}（没有新增）")
                    _stats["skipped"] += 1
                    continue
                label = prev.get("label") or label_for(st, agent, day, sess)
                under = f"{LIB_ROOT}/{agent}-raw/{day}"
                rel = f"{under}/{label}.md"
                try:
                    if not prev.get("file_id"):
                        # ① 新建：只在第一次见到这个会话时写一次（篇头 + 已有事件）
                        md = render_head(agent, day, label, sess) + render_events(new_evs)
                        stage = os.path.join(STAGING_DIR, agent, day, label)
                        os.makedirs(stage, exist_ok=True)
                        with open(os.path.join(stage, label + ".md"), "w", encoding="utf-8",
                                  newline="\n") as f:   # 别走 Windows 默认换行（落库会成 CRLF）
                            f.write(md)
                        r = lib_call("/api/import", {"src": stage, "under": under,
                                                     "overwrite": True, "rag": False}, "POST")
                        fid = (((r.get("created_ids") or []) + (r.get("updated_ids") or [])) or [""])[0]
                        if not fid:
                            fid = file_id_by_path(rel)
                        if not fid:
                            raise RuntimeError("导入回执没给 id，也没按路径问到：%s" % rel)
                        _stats["imported"] += 1
                        rep["created"].append(rel)
                        log(f"{key}：新建 {len(new_evs)} 条 → {rel}")
                    else:
                        # ② 追加：只渲染**新增那几条**，库那边接在文末（盘上按字节追加）
                        md = render_events(new_evs)
                        r = lib_call("/api/files/%s/append" % prev["file_id"],
                                     {"text": md}, "POST", timeout=300)
                        fid = prev["file_id"]
                        _stats["appended"] += 1
                        rep["appended"].append(f"{rel}（+{len(new_evs)} 条）")
                        log(f"{key}：追加 {len(new_evs)} 条 → {rel}")
                    _stats["events_in"] += len(new_evs)
                    _stats["bytes_out"] += len(md.encode("utf-8"))
                    _stats["assembled"] += 1
                    st[key] = {"file_id": fid, "label": label, "ok": True, "at": _stats["last_run"],
                               "events": len(sevs), "under": under, "session": sess,
                               "mode": "append" if prev.get("file_id") else "create"}
                    d = detail_ledger()
                    if d:
                        try:
                            d.append("flush", {"agent": agent, "day": day, "session": sess,
                                               "events": len(new_evs),
                                               "bytes": len(md.encode("utf-8")), "rel": rel})
                        except Exception:
                            pass
                except Exception as e:
                    st[key] = {"file_id": prev.get("file_id"), "label": label, "ok": False,
                               "at": _stats["last_run"], "events": prev.get("events"),
                               "error": f"{type(e).__name__}: {e}"}
                    rep["failed"].append(f"{key}: {type(e).__name__}: {e}")
                    _stats["failed"] += 1
                    _stats["last_error"] = f"{type(e).__name__}: {e}"
                    log(f"{key} 失败：{type(e).__name__}: {e}")
    save_state(st)
    _stats["agents"] = rep["agents"]
    return rep


# ── 平台调用面 ───────────────────────────────────────────────
def tool_wake(args):
    """agent 接口叫我们一声（它不等人，所以我们回得越快越好）"""
    _stats["wakes"] += 1
    with _lock:
        _wake["dirty"] = True
        _wake["due"] = time.time() + _DEBOUNCE   # 去抖：一串事件合成一次写
    return {"ok": True, "wakes": _stats["wakes"]}


def tool_flush(args):
    """立刻真跑一轮（验收脚本与"我现在就要看"用）。args: {agent?, day?, force?}"""
    args = args or {}
    rep = process(force=bool(args.get("force")),
                  only_agent=args.get("agent") or None,
                  only_day=args.get("day") or None)
    return {"ok": True, **rep}


def tool_status(_args=None):
    st = load_state()
    return {"stats": dict(_stats), "spool": SPOOL_DIR, "staging": STAGING_DIR,
            "state_keys": sorted(st.keys())[-12:], "lib": LIB_URL}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default=PLUGIN_ID)
    ap.add_argument("--interval", type=int, default=int(os.environ.get("TRANSCRIPT_INTERVAL") or 30))
    ap.add_argument("--debounce", type=float,
                    default=float(os.environ.get("TRANSCRIPT_DEBOUNCE") or 2.0),
                    help="被唤醒后合并多少秒内的事件再写一次（**不是等静默**：有就录，只是别重写十遍）")
    ap.add_argument("--quiet", type=int, default=None,
                    help="已废弃（2026-10-04 起「有就录」，不再等静默）")
    a = ap.parse_args(argv)
    global _DEBOUNCE
    _DEBOUNCE = max(0.2, float(a.debounce or 2.0))

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)
    os.makedirs(SPOOL_DIR, exist_ok=True)
    os.makedirs(STAGING_DIR, exist_ok=True)

    probe = Probe(a.id,
                  handlers={"transcript.wake": tool_wake,
                            "transcript.flush": tool_flush,
                            "transcript.status": tool_status},
                  stats_provider=lambda: dict(_stats), core_url=CORE_URL)
    probe.start()
    log(f"转录插件已启动：spool={SPOOL_DIR} → 库 {LIB_URL}（每 {a.interval}s 扫一轮兜底；"
        f"**spool 里有就录**，被唤醒后合并 {_DEBOUNCE:.1f}s 再写；"
        f"**一会话一篇、同会话只追加**，跨天才新建）")

    try:
        from probe import http_post    # 与 Probe 同一个前门地址与鉴权头
        http_post("/api/bridge/plugins/register", {
            "id": a.id, "name": "转录",
            "description": "把各 agent 接口（plugin.reasonix / plugin.dsh…）接住的日志记录，"
                           "组装成一天一份写进库（vault/日志/<agent>-raw/<日期>/会话.md），"
                           "盘上投影由库自己落。**内容是原文、不截断**；spool 在盘上，我挂了也不丢。",
            "capabilities": ["transcript.wake", "transcript.flush", "transcript.status"],
            "produces": [], "consumes": [],
        })
        log(f"注册成功 {a.id}")
    except Exception as e:
        log(f"注册失败（平台未就绪，探针周期会兜住）: {e}")

    parent_pid = os.getppid()
    next_parent = time.time() + 5
    next_register = 0.0
    while running:
        time.sleep(0.5)
        now = time.time()
        if now >= next_parent:
            next_parent = now + 5
            if not _pid_alive(parent_pid):
                log("父进程(core)已退出 → 自行退出，避免孤儿")
                break
        due = _wake["dirty"] and now >= _wake["due"]
        if due or now >= next_register:
            if due:
                with _lock:
                    _wake["dirty"] = False
                next_register = now + a.interval       # 被叫醒就顺带把这一轮当周期
            else:
                next_register = now + a.interval
            try:
                process()
            except Exception as e:
                _stats["errors"] += 1
                _stats["last_error"] = f"{type(e).__name__}: {e}"
                log(f"扫一轮失败（不影响下一轮）：{type(e).__name__}: {e}")

    try:
        probe.stop()
    except Exception:
        pass
    log(f"正常退出（组装 {_stats['assembled']} / 新建 {_stats['imported']} / "
        f"追加 {_stats['appended']} / 失败 {_stats['failed']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
