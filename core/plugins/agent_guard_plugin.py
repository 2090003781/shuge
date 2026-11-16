#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent 守护插件（external，**可选**）—— 谁没在跑，就把它拉起来。

辞玖 2026-10-05 定：
  · 做成**独立插件**（像注入包那样），不塞进接口插件里；
  · "我该负责起哪个 agent" = 描述文件（`agents/<id>.yaml`）里**声明了 `guard` 段**的才算我的；
    接口插件在平台组件表里代表那个 agent —— 本插件对着描述文件 + 组件表干活，这就是两者的"相连"；
  · **每个 agent 可选、默认关**：开关落在 `data/agents/guard.json`，控制台点一下立即生效（本插件不用重启）；
    插件本身也可以在控制台停掉（停了 = 全都先不管）。

判活（`guard.probe.kind`）：
  tcp  —— 连一下 127.0.0.1:port（DSH 就是 3080）
  proc —— 进程名在不在（tasklist；**只比名字，不猜命令行**）
  none —— 不守护（描述文件没写 guard 段就是这个）

拉起（`guard.launcher`）：直接 Popen，`CREATE_NO_WINDOW | DETACHED_PROCESS`
  —— **不弹黑窗是结构性保证**（跟内核 prepareCmd 一个路子），不是 best-effort。
杀掉（`guard.kill`，给 guard.restart 用）：按命令行匹配，PowerShell 静默执行（照样不弹窗）。

工具：
  guard.status            每个 agent：探针结果 / 开关 / 上次拉起 / 拉起次数
  guard.set {id, enabled} 开关（落盘，立即生效）
  guard.restart {id}      等价"重启"：杀掉 → 等守护把它拉回来（守护没开就直接报错，不偷偷帮你开）
  guard.tick              手动跑一轮（排查/自测）

两条克制：① 没写 guard 段的 agent 一律不碰；② 探针失败要**连续两次**才认定"没在跑"，
免得端口抖一下就误拉起。父进程（core）消失 → 自行退出（防孤儿）。
"""
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))
sys.path.insert(0, os.path.join(CORE_DIR, "scripts"))

try:
    import psutil                                   # 找命令行/杀进程用它，别去起 powershell
    HAS_PSUTIL = True
except Exception:
    HAS_PSUTIL = False

from probe import Probe, http_post        # noqa: E402
import agentctl as AC                     # noqa: E402

CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:8790")
PLUGIN_ID = "plugin.agent-guard"
STATE = os.path.join(CORE_DIR, "data", "agents", "guard.json")
LOGDIR = os.path.join(CORE_DIR, "data", "logs")
# CREATE_NO_WINDOW(0x08000000) | DETACHED_PROCESS(0x00000008)：既不弹窗也不新建控制台
NO_WINDOW = 0x08000000 | 0x00000008
MISS_TO_START = 2                          # 连续两次没在跑才拉（防抖）
COOLDOWN_SECONDS = 15                      # 拉过一次先等 15 秒，别追着拉
STALE_SECONDS = 90                         # 上次拉的还活着、却 90 秒没在跑 → 当僵死，清了重拉

running = True
INTERVAL = 3
_lock = threading.Lock()
_stats = {"ticks": 0, "starts": 0, "kills": 0, "errors": 0, "last_error": "", "last_tick": ""}


def log(msg):
    print("[guard] %s" % msg, flush=True)


def _read_state():
    if os.path.isfile(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}
    return {}


def _write_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, STATE)


def guarded():
    """我负责谁：描述文件里写了 guard 段的 agent（其余一律不碰）"""
    out = {}
    for aid, d in (AC.load_descs() or {}).items():
        g = d.get("guard") or {}
        kind = ((g.get("probe") or {}).get("kind") or "").lower()
        if kind and kind != "none":
            out[aid] = {"id": aid, "name": d.get("name") or aid,
                        "plugin_id": d.get("plugin_id") or ("plugin." + aid), "guard": g}
    return out


def probe_alive(g):
    """它在跑吗（探不明白就当"在跑" —— 宁可不动手）"""
    p = g.get("probe") or {}
    kind = (p.get("kind") or "none").lower()
    if kind == "tcp":
        try:
            with socket.create_connection(("127.0.0.1", int(p.get("port") or 0)), timeout=1.0):
                return True
        except Exception:
            return False
    if kind == "proc":
        name = (p.get("proc") or "").strip()
        if not name:
            return True
        if HAS_PSUTIL:
            try:
                return any((x.info.get("name") or "").lower() == name.lower()
                           for x in psutil.process_iter(["name"]))
            except Exception:
                return True
        try:
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % name, "/NH"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=10, creationflags=NO_WINDOW)
            return name.lower() in (r.stdout or "").lower()
        except Exception:
            return True
    return True


def launch(aid, g):
    """拉起来（Hidden：DETACHED_PROCESS + CREATE_NO_WINDOW）"""
    L = g.get("launcher") or {}
    cmd = AC.expand(L.get("cmd") or "")
    if not cmd:
        raise RuntimeError("描述文件里没写 guard.launcher.cmd（agents/%s.yaml）" % aid)
    args = [AC.expand(str(x)) for x in (L.get("args") or [])]
    cwd = AC.expand(L.get("cwd") or "") or CORE_DIR
    os.makedirs(LOGDIR, exist_ok=True)
    out = open(os.path.join(LOGDIR, "agent-%s.guard.out.log" % aid), "ab")
    err = open(os.path.join(LOGDIR, "agent-%s.guard.err.log" % aid), "ab")
    p = subprocess.Popen([cmd] + args, cwd=cwd, stdin=subprocess.DEVNULL,
                         stdout=out, stderr=err, creationflags=NO_WINDOW, close_fds=True)
    return p.pid


def _glob_hit(text, pat):
    """`*xxx*` 这种 glob 简化成"含不含"（够用，也躲开大小写坑）"""
    core = (pat or "").strip().strip("*")
    return bool(core) and (core.lower() in (text or "").lower())


def find_pids(proc, match, not_match=""):
    """按 进程名 + 命令行包含/不包含 找 pid。

    ⚠️ 为什么不用 PowerShell：实测**从 Python 里起 powershell.exe 会被整条静默拦掉**
    （`Write-Output START; <查询>; Write-Output END` 只回空、rc=0、无 stderr），
    原来那条 `Get-CimInstance … | Stop-Process` 永远匹配不到进程。
    """
    if not HAS_PSUTIL:
        raise RuntimeError("要装 psutil 才能按命令行找进程（pip install psutil）")
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = p.info.get("name") or ""
            cmd = " ".join(p.info.get("cmdline") or [])
            if proc and name.lower() != proc.lower():
                continue
            if match and not _glob_hit(cmd, match):
                continue
            if not_match and _glob_hit(cmd, not_match):
                continue
            out.append(int(p.info["pid"]))
        except Exception:
            continue
    return out


def kill_pid(pid):
    """按 pid 强杀（用来清掉自己拉起来但僵死的那个）"""
    try:
        if HAS_PSUTIL:
            psutil.Process(int(pid)).kill()
        else:
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/F"], capture_output=True,
                           creationflags=NO_WINDOW, timeout=20)
        _stats["kills"] += 1
        return True
    except Exception:
        return False


def kill_agent(aid, g):
    """按 guard.kill 的规则杀掉（给 restart 用）。返回杀掉的 pid 列表。"""
    k = g.get("kill") or {}
    if (k.get("kind") or "") != "cmdline":
        return []
    proc, match = k.get("proc") or "", k.get("match") or ""
    if not (proc and match):
        return []
    if not HAS_PSUTIL:
        raise RuntimeError("要装 psutil 才能按命令行杀进程（pip install psutil）")
    pids = find_pids(proc, match, k.get("not_match") or "")
    killed = []
    for pid in pids:
        try:
            psutil.Process(pid).kill()
            killed.append(pid)
        except Exception:
            pass
    _stats["kills"] += len(killed)
    return killed


def tick():
    """跑一轮：谁开关开着又没在跑 → 连两次就拉起来。返回每个 agent 一行状态。"""
    with _lock:
        st = _read_state()
        _stats["ticks"] += 1
        _stats["last_tick"] = time.strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for aid, info in guarded().items():
            g = info["guard"]
            row = dict(st.get(aid) or {})
            enabled = bool(row.get("enabled"))
            alive = probe_alive(g)
            now = time.time()
            if enabled and not alive:
                # 三条克制：① 刚拉过先等它起来（冷却）② 上次拉的那个进程还活着，就别重复拉
                # ③ 只有"活着但 90 秒都没在跑"才当僵死，清了重拉 —— 防"每几秒拉一次"的风暴
                row["down_since"] = float(row.get("down_since") or now)
                down = now - row["down_since"]
                row["miss"] = int(row.get("miss") or 0) + 1
                lp = int(row.get("last_pid") or 0)
                lp_alive = bool(lp) and _pid_alive(lp)
                if lp_alive and down < STALE_SECONDS:
                    row["warn"] = "上次拉的进程还在（pid=%s），探针却说没在跑 —— 先不重复拉（%ds）" % (lp, int(down))
                    row["miss"] = 0
                elif now < float(row.get("cooldown") or 0):
                    row["warn"] = "刚拉过，等它起来"
                    row["miss"] = 0
                elif row["miss"] >= MISS_TO_START:
                    if lp_alive:
                        log("%s 像僵死了（活着但 %ds 没在跑）→ 清掉重拉" % (aid, int(down)))
                        kill_pid(lp)
                    try:
                        pid = launch(aid, g)
                        row.update({"last_start": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "starts": int(row.get("starts") or 0) + 1,
                                    "last_pid": pid, "last_error": "", "warn": "",
                                    "down_since": 0, "cooldown": now + COOLDOWN_SECONDS})
                        _stats["starts"] += 1
                        log("拉起 %s（pid=%s）" % (aid, pid))
                    except Exception as e:
                        row["last_error"] = "%s: %s" % (type(e).__name__, e)
                        _stats["errors"] += 1
                        _stats["last_error"] = row["last_error"]
                        log("拉起 %s 失败：%s" % (aid, row["last_error"]))
                    row["miss"] = 0
            else:
                row["miss"] = 0
                row["down_since"] = 0
                if alive:
                    row["warn"] = ""
            row.update({"enabled": enabled, "alive": alive, "id": aid, "name": info["name"],
                        "plugin_id": info["plugin_id"], "probe": g.get("probe") or {},
                        "launcher": (g.get("launcher") or {}).get("cmd") or ""})
            st[aid] = {k: v for k, v in row.items()
                       if k in ("enabled", "miss", "alive", "last_start", "starts", "last_pid",
                                "last_error", "warn", "cooldown", "down_since")}
            rows.append(row)
        _write_state(st)
        return rows


# ── 工具面 ───────────────────────────────────────────────────
def tool_status(_args=None):
    return {"ok": True, "interval": INTERVAL, "state_file": STATE,
            "rows": tick(), "stats": dict(_stats)}


def tool_set(args=None):
    a = args or {}
    aid = str(a.get("id") or "")
    on = bool(a.get("enabled"))
    if aid not in guarded():
        return {"ok": False, "error": "这个 agent 没声明 guard（要在 agents/%s.yaml 里写 guard 段）" % aid}
    with _lock:
        st = _read_state()
        row = dict(st.get(aid) or {})
        row["enabled"] = on
        row["miss"] = 0
        row["alive"] = probe_alive(guarded()[aid]["guard"])
        st[aid] = row
        _write_state(st)
    log("守护 %s → %s" % (aid, "开" if on else "关"))
    return {"ok": True, "id": aid, "enabled": on, "alive": row["alive"]}


def tool_restart(args=None):
    a = args or {}
    aid = str(a.get("id") or "")
    info = guarded().get(aid)
    if not info:
        return {"ok": False, "error": "这个 agent 没声明 guard（agents/%s.yaml）" % aid}
    if not bool((_read_state().get(aid) or {}).get("enabled")):
        return {"ok": False, "error": "守护没开 —— 先打开守护，不然杀掉了没人拉它回来"}
    # **绝不在点名线程里等**：等 45 秒会把 poll 循环堵死 → 平台判 suspect → 把插件重启掉（真栽过）。
    # 杀完就返回，让调用方轮 guard.status 看结果。
    killed = kill_agent(aid, info["guard"])
    with _lock:
        st = _read_state()
        row = dict(st.get(aid) or {})
        row.update({"miss": 0, "down_since": 0, "cooldown": 0, "last_pid": 0})
        st[aid] = row
        _write_state(st)
    log("重启 %s：杀掉 %s，守护会在几秒内拉回来" % (aid, killed or "（没匹配到进程）"))
    return {"ok": bool(killed), "id": aid, "killed": killed,
            "note": "已杀掉；守护下一轮会拉回来，用 guard.status 看 alive",
            "error": "" if killed else "没匹配到要杀的进程（看 agents/%s.yaml 的 guard.kill）" % aid}


def tool_tick(_args=None):
    return {"ok": True, "rows": tick()}


def handle_stop(signum, frame):
    global running
    log("收到信号 %s，优雅退出" % signum)
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


def main(argv=None):
    global INTERVAL
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default=PLUGIN_ID)
    ap.add_argument("--interval", type=int, default=3, help="看一圈的间隔秒数")
    a = ap.parse_args(argv)
    INTERVAL = max(1, int(a.interval))
    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    probe = Probe(a.id, handlers={"guard.status": tool_status, "guard.set": tool_set,
                                  "guard.restart": tool_restart, "guard.tick": tool_tick},
                  stats_provider=lambda: dict(_stats), core_url=CORE_URL)
    probe.start()
    log("守护插件已启动（间隔 %ss，负责 %s；开关 %s）"
        % (INTERVAL, "、".join(guarded()) or "无", STATE))
    try:
        http_post("/api/bridge/plugins/register", {
            "id": a.id, "name": "Agent 守护",
            "description": "**可选**地替你看住 agent：描述文件里声明了 guard 段的，没在跑就静默拉起来"
                           "（DETACHED_PROCESS + CREATE_NO_WINDOW，结构性不弹窗）；每个 agent 一个开关，默认关。",
            "capabilities": ["guard.status", "guard.set", "guard.restart", "guard.tick"],
            "produces": [], "consumes": []})
        log("注册成功 %s" % a.id)
    except Exception as e:
        log("注册失败（平台未就绪，探针周期会兜住）: %s" % e)

    parent_pid = os.getppid()

    def loop():
        while running:
            try:
                tick()
            except Exception as e:
                _stats["errors"] += 1
                _stats["last_error"] = "%s: %s" % (type(e).__name__, e)
                log("tick 出错：%s" % _stats["last_error"])
            for _ in range(INTERVAL * 2):
                if not running:
                    break
                time.sleep(0.5)

    th = threading.Thread(target=loop, daemon=True, name="guard-loop")
    th.start()
    while running:
        time.sleep(0.5)
        if not _pid_alive(parent_pid):
            log("父进程(core)已退出 → 自行退出，避免孤儿")
            break
    try:
        probe.stop()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
