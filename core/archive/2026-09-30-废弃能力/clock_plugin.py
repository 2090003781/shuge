#!/usr/bin/env python3
"""标准时钟插件（external）——平台的报时源。

职责（辞玖 2026-09-06 定）：专责报时，向平台事件总线发布 clock.tick 事件；
定时任务/自动化可订阅该事件实现"到点干活"。

行为：
- 启动即发一次 clock.started（报身份与间隔）
- 之后每 --interval 秒（默认 60）发一次 clock.tick，payload 含 unix 时间/本地时间/序号
- 整点额外发 clock.hourly（便于做小时级任务）
- 收到 SIGTERM/SIGINT 优雅退出（发 clock.stopped 后退出）
- 父进程（core）消失 → 5 秒内自行退出（防孤儿，见下方父进程看护）
- 依赖：仅标准库（urllib + ctypes），无需安装任何东西

与 demo_heartbeat.py 的分工：heartbeat 是"监督机制测试样品"（验证崩溃重启），
本插件是"生产时钟源"（真干活）。

环境变量：
  CORE_URL       平台地址（默认 http://127.0.0.1:8790）
  CLOCK_INTERVAL 报时间隔秒（默认 60，可用 --interval 覆盖）
"""
import argparse
import json
import os
import signal
import sys
import time
import urllib.request
from datetime import datetime

# 心跳点名应答器（plugins/lib/probe.py）：平台按周期点名 → 回 pong/busy + 状态快照
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from probe import Probe  # noqa: E402

CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:8790")
DEFAULT_PLUGIN_ID = "plugin.clock"
running = True

# ── 父进程看护（2026-09-10 加）───────────────────────────────
# 背景：core 被强杀时（reasonix job 回收 / taskkill）没机会清理子进程，
# 插件会变成「孤儿」继续跑 —— 实测曾堆积 6 个孤儿，多个实例抢着报时。
# 对策：每 PARENT_CHECK_SECONDS 秒确认父进程还活着，父没了就自行退出。
# 零依赖：Windows 用 kernel32 API，其他平台回退 os.kill(pid, 0)。
PARENT_CHECK_SECONDS = 5
_PARENT_PID = os.getppid()


def _pid_alive(pid):
    """判断进程是否存活。"""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


# ── 心跳点名（2026-09-11）：平台按周期点名；一次拿活性 + 状态快照 ──
_PROBE = None
_tick_state = {"seq": 0, "last_at": 0.0, "interval": 60}


def _probe_stats():
    last = _tick_state["last_at"]
    return {
        "seq": _tick_state["seq"],
        "interval_s": _tick_state["interval"],
        "last_tick_ms_ago": int((time.time() - last) * 1000) if last else None,
    }


def start_probe(plugin_id):
    global _PROBE
    if _PROBE is not None:
        return _PROBE
    _PROBE = Probe(plugin_id, stats_provider=_probe_stats)
    _PROBE.start()
    return _PROBE


def handle_stop(signum, frame):
    global running
    running = False


def register_plugin(plugin_id, name, capabilities, produces=None, consumes=None):
    """向平台自报家门（协议：POST /api/bridge/plugins/register，幂等）
    produces/consumes 为事件拓扑声明（我产生/我消费哪些事件类型）
    """
    body = json.dumps({
        "id": plugin_id, "name": name, "capabilities": capabilities,
        "produces": produces or [], "consumes": consumes or [],
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        CORE_URL + "/api/bridge/plugins/register", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        print(f"[clock] 注册成功（isNew={data.get('isNew')}）", flush=True)
        return True
    except Exception as e:
        print(f"[clock] 注册失败（平台可能未就绪）: {e}", flush=True)
        return False


def post_event(plugin_id, ev_type, payload):
    """向平台上报事件（复用 bridge 入口，与 agent 同一条通路）"""
    body = json.dumps({
        "id": f"clock-{int(time.time()*1000)}",
        "agentId": plugin_id,
        "kind": "event",
        "event": {"type": ev_type, "source": plugin_id, "payload": payload},
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        CORE_URL + "/api/bridge/send", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        return True
    except Exception as e:
        print(f"[clock] 上报失败（平台可能未就绪）: {e}", flush=True)
        return False


def main():
    # running 是模块级退出标志（handle_stop 会置 False）。
    # 必须声明 global：下面第 152 行的 `running = False`（父进程消失分支）
    # 否则会让 Python 把 running 当成 main 的局部变量，
    # 使 `while running` 在赋值前读取 → UnboundLocalError
    # （2026-09-11 修复：该 bug 导致插件一启动就崩、被 supervisor 反复重启）。
    global running
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default=DEFAULT_PLUGIN_ID)
    ap.add_argument("--interval", type=float,
                    default=float(os.environ.get("CLOCK_INTERVAL", 60)))
    args = ap.parse_args()
    plugin_id = args.id

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    print(f"[clock] 启动 pid={os.getpid()} 间隔={args.interval}s core={CORE_URL}", flush=True)
    # 先自报家门（协议：注册身份 + 能力 + 事件拓扑声明），再开始报时
    registered = register_plugin(
        plugin_id, "标准时钟",
        ["clock.tick", "clock.hourly"],
        produces=["clock.tick", "clock.hourly", "clock.started", "clock.stopped"],
        consumes=[],  # 时钟目前不消费事件（纯发布源）
    )
    post_event(plugin_id, "clock.started", {
        "interval": args.interval, "pid": os.getpid(), "registered": registered,
    })

    _tick_state["interval"] = args.interval
    start_probe(plugin_id)

    seq = 0
    last_hour = None
    next_parent_check = time.time() + PARENT_CHECK_SECONDS
    while running:
        # 分片睡眠到下一个报时点（保证 SIGTERM 及时响应）
        deadline = time.time() + args.interval
        while running and time.time() < deadline:
            time.sleep(min(0.5, max(0.0, deadline - time.time())))
            # 父进程（core）没了就跟着退出，避免变成孤儿继续报时
            if time.time() >= next_parent_check:
                next_parent_check = time.time() + PARENT_CHECK_SECONDS
                if not _pid_alive(_PARENT_PID):
                    print("[clock] 父进程(core)已退出 → 自行退出，避免孤儿", flush=True)
                    running = False
        if not running:
            break

        seq += 1
        now = time.time()
        dt = datetime.now()
        _tick_state["seq"] = seq
        _tick_state["last_at"] = now
        payload = {
            "seq": seq,
            "unix": int(now),
            "local": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "hour": dt.hour,
        }
        post_event(plugin_id, "clock.tick", payload)
        print(f"[clock] tick #{seq} {payload['local']}", flush=True)
        # 整点事件（每小时一次）
        if dt.minute == 0 and dt.hour != last_hour:
            last_hour = dt.hour
            post_event(plugin_id, "clock.hourly", payload)

    post_event(plugin_id, "clock.stopped", {"seq": seq})
    print(f"[clock] 正常退出（共报时 {seq} 次）", flush=True)
    if _PROBE is not None:
        _PROBE.stop()
    sys.exit(0)


if __name__ == "__main__":
    main()
