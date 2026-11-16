#!/usr/bin/env python3
"""钩子转录服务（内核插件）—— 辞玖 2026-10-04 定的形状。

要说清的三件事：

**① 它凭什么能在内核里活**：它是 `config.yaml` 里 `plugins` 的一员，内核 spawn + Supervisor
管崩溃重启 + 点名心跳（探针长轮询）判卡死。事件触发替代不了这一层 —— 源不发声时，
"没事件"和"健康"长得一模一样（2026-10-03 12:30 就是这么静默死了 11 小时的）。

**② "连接 agent 接口"是个显式动作**：启动时从 `/api/components` 找到 `agent_id` 对应的
接口插件，调它的 `agent.attach.<agent>` 报到（"我负责你的日志"）。连上谁就录谁，
名单在接口那边查得到（`status.attachments`），要换人就 `agent.detach`。

**③ 日志走谁的手**：**走接口**（方案 A）。采集端只负责"把源读成事件"，然后成批交给
`agent.record.<agent>` —— 接口是该 agent 日志的唯一入口（落 spool、算它名下、唤醒组装），
采集端不自己写 spool。下游 `plugin.transcript` 一个字没改。

触发：**事件驱动优先 + 兜底轮询**
    · 直报型 agent（reasonix）：hook 一报，接口就唤醒我（真事件）
    · 文件型 agent（DSH）：**只 stat 的轻轮询**（默认 1 秒，66 个文件读 size/mtime，成本≈0），
      一变就拉；另留 60 秒兜底全扫（防"文件被换成同样大小的"这类漏判）
"""
import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))

from hook_sources import known_agents, source_for          # noqa: E402
from probe import Probe, auth_headers, http_post           # noqa: E402

CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:8790")
CORE_DIR = os.environ.get("CORE_DIR", os.path.dirname(HERE))
LIB_URL = (os.environ.get("AINOTES_URL") or "http://127.0.0.1:8848").rstrip("/")

PLUGIN_ID = "plugin.hook-transcript"
SPOOL_DIR = os.path.join(CORE_DIR, "data", "transcripts")
STAGING_DIR = os.path.join(SPOOL_DIR, ".out")
OLD_ERA_DIR = os.path.join(SPOOL_DIR, ".hook-push-era")     # 老的"推"那半条留下的 spool
STATE_FILE = os.path.join(SPOOL_DIR, ".hook-transcript-state.json")
ALARM_UNDER = "vault/系统/维护记录"
ALARM_NAME = "转录状态.md"
BATCH = 200                                                  # 一次交给接口多少条

running = True
_stats = {"runs": 0, "delivered": 0, "batches": 0, "pulls": 0, "errors": 0, "alarms": 0,
          "recovered": 0, "attaches": 0, "attached": [], "last_pull_at": "", "last_new_at": "",
          "last_error": "", "fast_ticks": 0, "deliver_fails": 0}
_CFG = {}
_lock = threading.Lock()
_wake = {"dirty": False, "due": 0.0, "agent": ""}


def log(msg):
    print(f"[hook-transcript] {msg}", flush=True)


def handle_stop(signum, frame):
    global running
    log(f"收到信号 {signum}，优雅退出")
    running = False


def _pid_alive(pid):
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
        return bool(ok) and code.value == 259
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ── 状态 ─────────────────────────────────────────────────────
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


# ── 平台：找接口、挂接、投递 ─────────────────────────────────
def components():
    """内核里的组件表（含 agent_id → 背书它的插件 id）"""
    req = urllib.request.Request(CORE_URL + "/api/components", headers=auth_headers())
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def find_interface(agent_id):
    """这个 agent 的接口插件是谁（不写死：从组件表里按 agent_id 找）"""
    for c in components():
        if c.get("agent_id") == agent_id:
            return c.get("id") or ""
    return ""


def call_plugin(plugin, tool, args, timeout_ms=60000):
    """经插件通道调另一个插件（内核负责转发）"""
    return http_post("/api/bridge/plugins/call",
                     {"plugin": plugin, "tool": tool, "args": args, "timeoutMs": timeout_ms},
                     timeout=max(15, timeout_ms / 1000.0 + 10))


def attach_agent(state, agent_id, note=""):
    """连上某个 agent 接口（连上就要开工）。返回 (ok, 回执/错误)"""
    iface = state.setdefault("attached", {}).get(agent_id, {}).get("plugin") or find_interface(agent_id)
    if not iface:
        return False, "组件表里没找到 %s 的接口插件" % agent_id
    sfx = agent_id.rsplit(".", 1)[-1]
    st, d = _post_call(iface, "agent.attach." + sfx,
                       {"recorder": PLUGIN_ID, "note": note or "钩子转录服务"})
    res = _payload(d)
    if st == 200 and res.get("ok"):
        state["attached"][agent_id] = {"plugin": iface, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                       "record_tool": res.get("record_tool") or
                                       ("agent.record." + sfx),
                                       "status_tool": res.get("status_tool") or "",
                                       "spool_dir": res.get("spool_dir") or ""}
        _stats["attaches"] += 1
        _stats["attached"] = sorted(state["attached"])
        log(f"已连上 {agent_id}（接口 {iface}）：record_tool={res.get('record_tool')}，"
            f"spool={res.get('spool_dir')}，接口那边最后一条日志：{res.get('last_record_at') or '（还没有）'}")
        return True, res
    return False, d.get("error") or ("HTTP %s" % st)


def _payload(d):
    """插件调用回执里，工具的返回值在 **`data`**（老代码按 `result` 取——取不到就以为失败了）"""
    if not isinstance(d, dict):
        return {}
    return d.get("data") or d.get("result") or {}


def _post_call(plugin, tool, args, timeout_ms=60000):
    """POST 一次插件调用，返回 (状态码, 回执)；失败不抛，交给调用方处置"""
    try:
        body = json.dumps({"plugin": plugin, "tool": tool, "args": args,
                           "timeoutMs": timeout_ms}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(CORE_URL + "/api/bridge/plugins/call", data=body, method="POST",
                                     headers=auth_headers({"Content-Type": "application/json"}))
        with urllib.request.urlopen(req, timeout=max(15, timeout_ms / 1000.0 + 10)) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:
        return 0, {"error": "%s: %s" % (type(e).__name__, e)}


def deliver(state, agent_id, events):
    """把事件成批交给 agent 接口（**方案 A：日志走接口的手**）"""
    info = (state.get("attached") or {}).get(agent_id) or {}
    iface, tool = info.get("plugin"), info.get("record_tool")
    if not iface or not tool:
        return 0, "还没连上 %s" % agent_id
    done = 0
    for i in range(0, len(events), BATCH):
        chunk = events[i:i + BATCH]
        st, d = _post_call(iface, tool, {"events": chunk})
        res = _payload(d)
        if st == 200 and res.get("ok"):
            done += int(res.get("recorded") or 0)
            _stats["batches"] += 1
        else:
            _stats["deliver_fails"] += 1
            return done, "交给接口失败：%s" % (res.get("error") or d.get("error") or st)
    return done, ""


# ── 看门狗：源在动、接口却没进新日志 ─────────────────────────
def _alarm_text(state, agent_id, kind, since_s, hint):
    head = ("# DSH 转录中断\n\n> **%s 起，%s 的源看着在动，可日志已经 %d 秒没进来了。**"
            % (state.get("alarm", {}).get("since") or "?", agent_id, int(since_s))) if kind == "alarm" \
        else "# 转录已恢复\n\n> **%s 恢复**：%s 的日志又进来了。" % (time.strftime("%Y-%m-%d %H:%M:%S"), agent_id)
    info = (state.get("attached") or {}).get(agent_id) or {}
    lines = [head, "", "| 看什么 | 值 |", "|---|---|",
             "| 谁在录 | `%s`（内核插件，Supervisor 管重启） |" % PLUGIN_ID,
             "| 挂在谁身上 | `%s`（接口插件 `%s`） |" % (agent_id, info.get("plugin") or "?"),
             "| 这一源的读法 | %s |" % hint,
             "| 上一批日志 | %s |" % (state.get("sources", {}).get(agent_id, {}).get("last_new_at") or "（还没有）"),
             "| 这次检查 | %s |" % time.strftime("%Y-%m-%d %H:%M:%S")]
    if kind == "alarm":
        lines += ["", "## 先看这几处", "",
                  "1. 我挂在接口上了吗：接口的 `status.attachments` 里有没有 `%s`；" % PLUGIN_ID,
                  "2. `data/logs/plugin.hook-transcript.log` 里这一轮报了什么（读失败会打出来）；",
                  "3. 源本身还在长吗（文件型的看 mtime；直报型的看 hook 有没有上报）。"]
    return "\n".join(lines) + "\n"


def write_alarm_note(text):
    stage = os.path.join(STAGING_DIR, "hook-transcript")
    os.makedirs(stage, exist_ok=True)
    with open(os.path.join(stage, ALARM_NAME), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    body = json.dumps({"src": stage, "under": ALARM_UNDER, "overwrite": True, "rag": False},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(LIB_URL + "/api/import", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Writer": "hook-transcript"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def check_watchdog(state, now):
    """源在动、却没新日志进来 → 告警（只报，不干活）"""
    rep = {"alarm": "", "note": ""}
    for agent_id, src in _SOURCES.items():
        try:
            active = src.activity_hint()
        except Exception:
            active = None
        if active is None:
            continue                     # 看不出来就别下结论
        sstate = state.setdefault("sources", {}).setdefault(agent_id, {})
        last = sstate.get("last_new_epoch") or now
        if (now - last) <= _CFG.get("stall", 1200):
            continue
        key = "alarm:" + agent_id
        if state.get("alarm", {}).get(key):
            continue                     # 已经告过，别刷屏
        state.setdefault("alarm", {})[key] = True
        state["alarm"]["since"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last))
        _stats["alarms"] += 1
        rep["alarm"] = agent_id
        log("⚠️ 看门狗：%s 的源看着在动，却已 %.0f 秒没有新日志（写告警）" % (agent_id, now - last))
        try:
            write_alarm_note(_alarm_text(state, agent_id, "alarm", now - last,
                                         getattr(src, "kind", "?")))
            rep["note"] = "ok"
        except Exception as e:
            log("告警纸写库失败（不影响拉取）: %s: %s" % (type(e).__name__, e))
    return rep


def clear_alarm(state, agent_id):
    key = "alarm:" + agent_id
    if (state.get("alarm") or {}).get(key):
        state["alarm"][key] = False
        _stats["recovered"] += 1
        log("✅ 看门狗：%s 又有日志了，告警解除" % agent_id)
        try:
            write_alarm_note(_alarm_text(state, agent_id, "recovered", 0, ""))
        except Exception as e:
            log("恢复纸写库失败（忽略）: %s" % e)


# ── 一轮拉取 ─────────────────────────────────────────────────
_SOURCES = {}


def pull_agent(state, agent_id, force=False):
    """把某个 agent 的新记录读出来交给接口。返回报告。"""
    src = _SOURCES.get(agent_id)
    if src is None or getattr(src, "kind", "") != "file":
        return {"new": 0, "skipped": "该 agent 的日志由 hook 直报接口，我不用采"}
    sstate = state.setdefault("sources", {}).setdefault(agent_id, {})
    if not force:
        sig = src.signature()
        if sig == sstate.get("sig"):
            return {"new": 0, "skipped": "源没变"}
        sstate["sig"] = sig
    _stats["pulls"] += 1
    events, rep = src.poll(sstate)
    _stats["last_pull_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if rep.get("errors"):
        for e in rep["errors"]:
            _stats["errors"] += 1
            log("读会话出错：%s" % e)
    if not events:
        sstate["sig"] = src.signature()
        return {"new": 0, "read": 0, "errors": rep.get("errors") or []}
    ok, err = deliver(state, agent_id, events)
    _stats["delivered"] += ok
    if err:
        _stats["last_error"] = err
        log("⚠️ %s" % err)
        return {"new": 0, "read": len(events), "error": err}
    sstate["last_new_epoch"] = time.time()
    sstate["last_new_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    sstate["sig"] = src.signature()
    _stats["last_new_at"] = sstate["last_new_at"]
    clear_alarm(state, agent_id)
    log("拉到 %d 条 → 已交给 %s（spool 由接口落）" % (ok, agent_id))
    return {"new": ok, "read": len(events), "days": rep.get("days") or {}}


def takeover(state):
    """老的"推"那条路留下的 spool 挪走（同一天两份串味）。只做一次。"""
    if state.get("took_over"):
        return []
    moved = []
    for agent_id, info in (state.get("attached") or {}).items():
        spool = info.get("spool_dir") or ""
        if not spool or not os.path.isdir(spool):
            continue
        dst = os.path.join(OLD_ERA_DIR, agent_id)
        try:
            os.makedirs(dst, exist_ok=True)
            for name in os.listdir(spool):
                if not name.endswith(".jsonl"):
                    continue
                since = _CFG.get("since") or ""
                if since and name[:-6] < since:
                    continue
                src_path, dst_path = os.path.join(spool, name), os.path.join(dst, name)
                try:
                    os.replace(src_path, dst_path)
                except OSError:
                    # WinError 17：**os.replace 不能跨盘**（源在别的盘/临时目录时就会撞）
                    import shutil as _sh
                    _sh.move(src_path, dst_path)
                moved.append("%s/%s" % (agent_id, name))
        except Exception as e:
            log("旧 spool 挪走失败（忽略）: %s" % e)
    state["took_over"] = True
    if moved:
        log("旧 spool 挪到 %s：%s" % (OLD_ERA_DIR, ", ".join(moved)))
    return moved


# ── 平台调用面 ───────────────────────────────────────────────
def tool_status(_args=None):
    return {"stats": dict(_stats), "agents": sorted(_SOURCES), "state_file": STATE_FILE,
            "attached": load_state().get("attached") or {}, "lib": LIB_URL}


def tool_flush(args=None):
    args = args or {}
    state = load_state()
    reps = {}
    for agent_id in _SOURCES:
        reps[agent_id] = pull_agent(state, agent_id, force=True)
    save_state(state)
    if any((r or {}).get("new") for r in reps.values()):
        try:
            http_post("/api/bridge/plugins/call",
                      {"plugin": "plugin.transcript", "tool": "transcript.wake",
                       "args": {"agent": args.get("agent") or "", "day": args.get("day") or ""},
                       "timeoutMs": 1500}, timeout=8)
            _stats["wakes"] = _stats.get("wakes", 0) + 1
        except Exception:
            pass
    return {"ok": True, "reports": reps}


def tool_wake(args=None):
    """接口收到上报后叫我一声（真事件）"""
    a = args or {}
    with _lock:
        _wake["dirty"] = True
        _wake["due"] = time.time() + 0.5
        _wake["agent"] = a.get("agent") or ""
    return {"ok": True}


def tool_attach(args=None):
    """手动挂接（诊断用）：`{"agent": "agent.dsh"}` 或 `{"agents": [...]}`"""
    a = args or {}
    want = a.get("agents") or ([a["agent"]] if a.get("agent") else sorted(_SOURCES))
    state = load_state()
    out = {}
    for agent_id in want:
        ok, res = attach_agent(state, agent_id, note=a.get("note") or "")
        out[agent_id] = res if ok else {"ok": False, "error": res}
    save_state(state)
    return {"ok": all((v or {}).get("ok") for v in out.values()), "result": out}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default=PLUGIN_ID)
    ap.add_argument("--agents", default="agent.dsh",
                    help="要接哪些 agent 的日志（逗号分隔）；默认 agent.dsh")
    ap.add_argument("--fast", type=float, default=1.0,
                    help="文件型源的轻轮询间隔（只 stat，默认 1 秒）")
    ap.add_argument("--interval", type=int, default=60,
                    help="兜底全扫间隔（防漏判，默认 60 秒）")
    ap.add_argument("--stall", type=int, default=1200, help="源在动但多久没新日志就告警（秒）")
    ap.add_argument("--since", default="", help="首次从哪天起读（YYYY-MM-DD；进度记在状态里）")
    ap.add_argument("--takeover", action="store_true", help="首次把老「推」那条路的 spool 挪走")
    ap.add_argument("--sessions-dir", default="")
    ap.add_argument("--with-reasoning", action="store_true")
    ap.add_argument("--tool-result-max", type=int, default=8000)
    ap.add_argument("--tool-args-max", type=int, default=2000)
    a = ap.parse_args(argv)

    global _CFG
    _CFG = {"since": a.since, "stall": a.stall, "takeover": a.takeover}
    wanted = [x.strip() for x in (a.agents or "").split(",") if x.strip()]
    for agent_id in wanted:
        src = source_for(agent_id, sessions_dir=(a.sessions_dir or None), since=a.since,
                         with_reasoning=a.with_reasoning, tool_result_max=a.tool_result_max,
                         tool_args_max=a.tool_args_max)
        if src is None:
            log(f"⚠️ 不认识 {agent_id}：没有它的源适配器，跳过（可读的：{known_agents()}）")
            continue
        _SOURCES[agent_id] = src
    if not _SOURCES:
        log("没有可接的 agent，退出")
        return 2

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)
    os.makedirs(SPOOL_DIR, exist_ok=True)
    os.makedirs(STAGING_DIR, exist_ok=True)

    probe = Probe(a.id, handlers={"hooktrans.status": tool_status, "hooktrans.flush": tool_flush,
                                  "hooktrans.wake": tool_wake, "hooktrans.attach": tool_attach},
                  stats_provider=lambda: dict(_stats), core_url=CORE_URL)
    probe.start()
    log("钩子转录服务已启动：接 %s（内核监督；触发=事件优先 + %.1fs 轻轮询 + %ds 兜底）"
        % (", ".join(sorted(_SOURCES)), a.fast, a.interval))
    try:
        http_post("/api/bridge/plugins/register", {
            "id": a.id, "name": "钩子转录服务",
            "description": "把某个 agent 的日志源读成事件，成批交给它的 agent 接口"
                           "（agent.record.<agent>）→ spool → plugin.transcript 组装入库。"
                           "**跑在内核里、由内核监督保活**；启动时对每个 --agents 调 "
                           "agent.attach.<agent> 连上接口（连上谁就录谁），源在动却没日志进来会写告警。",
            "capabilities": ["hooktrans.status", "hooktrans.flush", "hooktrans.wake",
                             "hooktrans.attach"], "produces": [], "consumes": []})
        log(f"注册成功 {a.id}")
    except Exception as e:
        log(f"注册失败（平台未就绪，探针周期会兜住）: {e}")

    # ① 连上接口（连上才开工）
    state = load_state()
    for agent_id in _SOURCES:
        try:
            ok, res = attach_agent(state, agent_id)
            if not ok:
                log(f"连 {agent_id} 失败（会重试）：{res}")
        except Exception as e:
            log(f"连 {agent_id} 出错（会重试）：{type(e).__name__}: {e}")
    if a.takeover:
        takeover(state)
    save_state(state)

    parent_pid = os.getppid()
    next_parent = time.time() + 5
    next_fast = 0.0
    next_full = 0.0
    next_attach = time.time() + 60
    next_watch = time.time() + 30
    while running:
        time.sleep(0.25)
        now = time.time()
        if now >= next_parent:
            next_parent = now + 5
            if not _pid_alive(parent_pid):
                log("父进程(core)已退出 → 自行退出，避免孤儿")
                break
        # 兜底：接口重启后挂接会丢，定期补挂（幂等）
        if now >= next_attach:
            next_attach = now + 60
            for agent_id in _SOURCES:
                if not (state.get("attached") or {}).get(agent_id):
                    try:
                        attach_agent(state, agent_id)
                    except Exception:
                        pass
        with _lock:
            woke = _wake["dirty"] and now >= _wake["due"]
            if woke:
                _wake["dirty"] = False
        if woke:
            for agent_id in _SOURCES:
                try:
                    pull_agent(state, agent_id, force=True)
                except Exception as e:
                    _stats["errors"] += 1
                    _stats["last_error"] = "%s: %s" % (type(e).__name__, e)
            save_state(state)
            continue
        if now >= next_fast:
            next_fast = now + max(0.2, a.fast)
            _stats["fast_ticks"] += 1
            touched = False
            for agent_id, src in _SOURCES.items():
                if getattr(src, "kind", "") != "file":
                    continue
                sstate = state.setdefault("sources", {}).setdefault(agent_id, {})
                try:
                    if src.signature() == sstate.get("sig"):
                        continue
                except Exception:
                    pass
                try:
                    touched = pull_agent(state, agent_id) or touched
                except Exception as e:
                    _stats["errors"] += 1
                    _stats["last_error"] = "%s: %s" % (type(e).__name__, e)
                    log("这一轮失败（不影响下一轮）：%s: %s" % (type(e).__name__, e))
            if touched:
                save_state(state)
        if now >= next_full:
            next_full = now + a.interval
            for agent_id in _SOURCES:
                try:
                    pull_agent(state, agent_id, force=True)
                except Exception as e:
                    _stats["errors"] += 1
            save_state(state)
        if now >= next_watch:
            next_watch = now + 30
            try:
                check_watchdog(state, now)
                save_state(state)
            except Exception as e:
                log("看门狗出错（忽略）: %s: %s" % (type(e).__name__, e))

    try:
        probe.stop()
    except Exception:
        pass
    log("正常退出（交付 %d 条 / 拉取 %d 轮 / 告警 %d 次）"
        % (_stats["delivered"], _stats["pulls"], _stats["alarms"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
