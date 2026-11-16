#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""守护插件自测 —— 全程对着**假 agent**（一个只监听端口的假进程 + 临时描述文件），
绝不碰真的 DSH（真的在 3080，自测用 3199）。

验七条：
  T1 默认关：开关关着时，它死了也不管
  T2 开了就拉：打开守护 → 它自己把假的拉起来（说明探针判定 + 拉起都对）
  T3 还活着不重复拉：进程没变、拉起次数没涨
  T4 被杀自动拉回来：直接杀掉 → 守护下一轮把它拉回来
  T5 guard.restart：杀掉 + 等它回来（等价"重启"）
  T6 关掉就撒手：关掉守护 → 杀掉 → 不拉
  T7 台账对得上：status 里能看到它、开关状态一致

跑：python scripts\\smoke_agent_guard.py      退出码 0 = 全过
"""
import io
import json
import os
import socket
import subprocess
import sys
import time

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(CORE, "scripts"))
import agentctl as AC   # noqa: E402

PLUGIN = "plugin.agent-guard"
AID = "zz-guard-test"
PORT = 3199
NO_WINDOW = 0x08000000 | 0x00000008
FAKE = os.path.join(CORE, "data", "tmp", "zz-guard-fake-agent.py")
DESC = os.path.join(CORE, "agents", AID + ".yaml")
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  ← " + str(extra)))
    if not cond:
        fails.append(name)


def call(tool, args=None):
    _st, d = AC.http("/api/bridge/plugins/call",
                     {"plugin": PLUGIN, "tool": tool, "args": args or {}, "timeoutMs": 60000},
                     "POST", timeout=180)
    d = d or {}
    return d.get("data") if isinstance(d.get("data"), dict) else d      # 桥接回执是 {callId, data, ok}


def listening(port=PORT):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except Exception:
        return False


import psutil   # noqa: E402  找/杀进程用它（别起 powershell：从 Python 起会被整条静默拦掉）


def fake_pids():
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if "zz-guard-fake" in " ".join(p.info.get("cmdline") or []):
                out.append(int(p.info["pid"]))
        except Exception:
            continue
    return out


def kill_fake():
    for pid in fake_pids():
        try:
            psutil.Process(pid).kill()
        except Exception:
            pass
    time.sleep(1)


def wait_until(cond, seconds, step=1.0):
    t0 = time.time()
    while time.time() - t0 < seconds:
        if cond():
            return True, round(time.time() - t0, 1)
        time.sleep(step)
    return False, round(time.time() - t0, 1)


def row_of(status_data):
    for r in (status_data.get("rows") or []):
        if r.get("id") == AID:
            return r
    return {}


def ensure_plugin():
    d = call("guard.status")
    if d.get("ok"):
        return True
    AC.http("/api/plugins/start", {"id": PLUGIN}, "POST", timeout=60)
    ok, _ = wait_until(lambda: call("guard.status").get("ok"), 30)
    return ok


def main():
    print("守护插件自测（假 agent，端口 %d）" % PORT)
    print("-" * 56)
    if listening():
        print("端口 %d 已经被占 —— 先让出来再测" % PORT)
        return 1
    if not ensure_plugin():
        print("守护插件没跑起来：config.yaml 里有 plugin.agent-guard 条目吗？控制台插件列表里能启用吗？")
        return 1

    # 假 agent（只监听端口）+ 临时描述文件（声明 guard 段）
    os.makedirs(os.path.dirname(FAKE), exist_ok=True)
    io.open(FAKE, "w", encoding="utf-8", newline="\n").write(
        "import argparse, socket, time\n"
        "ap = argparse.ArgumentParser(); ap.add_argument('--port', type=int); a = ap.parse_args()\n"
        # 故意不设 SO_REUSEADDR：Windows 上它允许同一端口被绑多次，端口归属会飘，探针判断就不准
        "s = socket.socket()\n"
        "s.bind(('127.0.0.1', a.port)); s.listen(5)\n"
        "while True: time.sleep(1)\n")
    io.open(DESC, "w", encoding="utf-8", newline="\n").write(
        "# 自测用（zz- 前缀，跑完自删）：一个假 agent，只监听端口\n"
        "id: %s\nname: zz 守护自测（假 agent）\nagent_id: agent.%s\nplugin_id: plugin.%s\n"
        "detect: {paths: [], files: [], exes: []}\n"
        "mcp: {kind: none}\nhook: {kind: none}\n"
        "guard:\n  probe: {kind: tcp, port: %d}\n"
        "  launcher:\n    cmd: \"python\"\n    args: [\"%s\", \"--port\", \"%d\"]\n    cwd: \"%s\"\n"
        "  kill: {kind: cmdline, proc: python.exe, match: \"*zz-guard-fake*\"}\n"
        % (AID, AID, AID, PORT, FAKE.replace("\\", "/"), PORT,
           os.path.dirname(FAKE).replace("\\", "/")))
    kill_fake()

    try:
        # T1 默认关：死了也不管
        call("guard.set", {"id": AID, "enabled": False})
        kill_fake()
        time.sleep(9)
        check("T1 默认关：关着时死了也不拉", not listening(), "端口竟然在听（不该）")

        # T2 开了就拉
        call("guard.set", {"id": AID, "enabled": True})
        ok, sec = wait_until(listening, 20)
        check("T2 开了就拉：它自己把假的拉起来了（%.1fs）" % sec, ok, "20 秒没起来")
        pid1 = fake_pids()[0] if fake_pids() else 0
        starts1 = int(row_of(call("guard.status")).get("starts") or 0)
        check("T2b 台账记了这次拉起", starts1 >= 1, "starts=" + str(starts1))

        # T3 还活着不重复拉
        time.sleep(8)
        got = fake_pids()
        starts2 = int(row_of(call("guard.status")).get("starts") or 0)
        check("T3 活着就不重复拉", got and got[0] == pid1 and starts2 == starts1,
              "pid %s→%s / starts %s→%s" % (pid1, got[0] if got else None, starts1, starts2))

        # T4 杀了自动拉回来
        kill_fake()
        ok, sec = wait_until(listening, 25)
        check("T4 被杀后自动拉回来（%.1fs）" % sec, ok, "25 秒没回来")
        starts3 = int(row_of(call("guard.status")).get("starts") or 0)
        check("T4b 拉起次数又涨了", starts3 > starts2, "starts=%s" % starts3)

        # T5 guard.restart（杀完立刻返回，不该阻塞点名 —— 阻塞会被平台当 suspect 重启插件）
        t0 = time.time()
        r = call("guard.restart", {"id": AID})
        took = round(time.time() - t0, 1)
        check("T5 guard.restart：很快就回（%.1fs，没阻塞）" % took, took < 10, "花了 %.1fs" % took)
        check("T5b restart 报了杀掉的 pid", bool(r.get("killed")), json.dumps(r, ensure_ascii=False)[:200])
        ok5, sec5 = wait_until(listening, 30)
        check("T5c restart 后守护把它拉回来了（%.1fs）" % sec5, ok5, "30 秒没回来")
        time.sleep(8)
        check("T5d 没被重复拉起（防风暴）", len(fake_pids()) == 1, "存活 %d 个假进程" % len(fake_pids()))

        # T6 关掉就撒手
        call("guard.set", {"id": AID, "enabled": False})
        kill_fake()
        time.sleep(9)
        check("T6 关掉就撒手：不再拉", not listening(), "端口还在听（不该）")

        # T7 台账对得上
        st = call("guard.status")
        row = row_of(st)
        check("T7 status 里有它、开关=关",
              bool(row) and row.get("enabled") is False and row.get("probe", {}).get("port") == PORT,
              json.dumps(row, ensure_ascii=False)[:200])
        # T8 真 DSH 的 kill 规则**只读**对准（不杀）：命中的必须都含 deepseek-ai\dsh、且不含 subprocess-local
        sys.path.insert(0, os.path.join(CORE, "plugins"))
        import agent_guard_plugin as G      # 只借它的 find_pids（纯查，不动手）
        import psutil as _ps
        _k = ((AC.load_descs().get("dsh") or {}).get("guard") or {}).get("kill") or {}
        _pids = G.find_pids(_k.get("proc") or "", _k.get("match") or "", _k.get("not_match") or "")
        _cmds = []
        for _pid in _pids:
            try:
                _cmds.append(" ".join(_ps.Process(_pid).cmdline()))
            except Exception:
                pass
        check("T8 真 DSH 的 kill 规则对准（只读；命中都含 deepseek-ai\\dsh、不含 subprocess-local）",
              bool(_pids) and all(("deepseek-ai\\dsh" in c.lower() and "subprocess-local" not in c.lower())
                                  for c in _cmds),
              "pids=%s cmds=%s" % (_pids, [c[:70] for c in _cmds]))

    finally:
        # 清场：关开关、杀假的、删临时描述文件与状态条目
        try:
            call("guard.set", {"id": AID, "enabled": False})
        except Exception:
            pass
        kill_fake()
        for p in (DESC, FAKE):
            try:
                os.remove(p)
            except Exception:
                pass
        try:
            st = AC.load_guard_state()
            st.pop(AID, None)
            AC.save_guard_state(st)
        except Exception:
            pass
        time.sleep(4)      # 让插件下一轮把它从台账里去掉

    print("-" * 56)
    if fails:
        print("失败 %d 条：%s" % (len(fails), "、".join(fails)))
        return 1
    print("全部通过（假 agent 已清场，真 DSH 没碰）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
