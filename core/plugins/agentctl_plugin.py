#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent 接入插件（内核侧）—— 把 `scripts/agentctl.py` 那套包成平台调用面，给控制台用。

辞玖 2026-10-05：这套"自动识别 → 勾选 → 生成"要在**内核控制台**里能用（置顶面板 + 弹窗）。

工具面（控制台经 `/api/bridge/plugins/call` 调）：
    agents.scan    自动识别本机上的 agent（描述文件里的 + 候选表命中的 + 模糊发现的疑似配置）
    agents.add     勾选确认后真接入（写 config.yaml / 登记档位 / 写 agent 侧 MCP·hook）
    agents.prompt  生成**给那个 agent 的提示词**（可复制；告诉它工具怎么用、剩下那半截怎么接）
    agents.verify  验收（接口在跑 / 能力齐 / 档位 / 真调一次 brain.health / MCP 在位）
    agents.status  我这个插件自己的状态
"""
import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))
sys.path.insert(0, os.path.join(CORE_DIR, "scripts"))

from probe import Probe, http_post        # noqa: E402
import agentctl as AC                     # noqa: E402

CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:8790")
PLUGIN_ID = "plugin.agentctl"
running = True
_stats = {"scans": 0, "adds": 0, "prompts": 0, "verifies": 0, "errors": 0, "last_error": ""}


def log(msg):
    print(f"[agentctl] {msg}", flush=True)


def _pid_alive(pid):
    """父进程（core）还在不在（与别的插件同款，跨平台）"""
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


def handle_stop(signum, frame):
    global running
    log(f"收到信号 {signum}，优雅退出")
    running = False


def _ops_of(plugin_id):
    """这个 agent 接口现在放开的操作（控制台里那份提示词要按实际档位写）"""
    try:
        st, d = AC.http("/api/ops?plugin=" + plugin_id)
        ops = [(o or {}).get("id") for o in (d.get("ops") or []) if (o or {}).get("released")]
        st2, pd = AC.http("/api/permissions?plugin=" + plugin_id)
        return ops, ((pd.get("byPlugin") or {}).get(plugin_id, {}) or {}).get("level") \
            or pd.get("level") or ""
    except Exception:
        return [], ""


# ── 工具面 ───────────────────────────────────────────────────
# 「手工单执行报告信箱」（data/onboard/*.md）2026-10-06 拆掉（辞玖：在别人电脑上不会有反馈给你）——
# 对方多半根本写不到我们盘上，留这个约定只会让人以为"交了作业"，不如让它把证据留在自己那边。
def tool_scan(_args=None):
    """**自动识别**：本机上有哪些 agent（含没进表的疑似配置）"""
    _stats["scans"] += 1
    try:
        agents = AC.scan_all()
        connected = [a["id"] for a in agents if a.get("connected")]
        attachable = [a["id"] for a in agents
                      if a.get("has_desc") and a.get("detected") and not a.get("connected")]
        return {"ok": True, "count": len(agents), "connected": connected,
                "attachable": attachable, "console_risks": AC.console_risks(),
                "agents": agents,
                "templates": os.path.join(CORE_DIR, "templates", "agent", "README.md")}
    except Exception as e:
        _stats["errors"] += 1
        _stats["last_error"] = "%s: %s" % (type(e).__name__, e)
        return {"ok": False, "error": _stats["last_error"]}


def tool_add(args=None):
    """勾选确认后**真接入**（args: {ids: [...], apply: true}）"""
    a = args or {}
    ids = [str(x) for x in (a.get("ids") or [])]
    apply_ = bool(a.get("apply", True))
    descs = AC.load_descs()
    out = []
    for aid in ids:
        d = descs.get(aid)
        if not d:
            out.append({"id": aid, "ok": False, "error": "没有描述文件 agents/%s.yaml" % aid})
            continue
        row = {"id": aid, "name": d.get("name") or aid, "steps": []}
        try:
            want = AC.tpl_args_line(d)
            pid = d["plugin_id"]
            if not AC.config_has(pid):
                if apply_:
                    AC.insert_plugin_entry(pid, AC.render_plugin_entry(d))
                row["steps"].append({"step": "内核侧接口", "did": "插入" if apply_ else "会插入"})
            elif apply_ and AC.replace_in_block(
                    AC.CONFIG, lambda l: l.strip() == "- id: %s" % pid,
                    lambda l: l.strip().startswith("args:"), "    " + want):
                row["steps"].append({"step": "内核侧接口", "did": "已按模板更新"})
            else:
                row["steps"].append({"step": "内核侧接口", "did": "已是模板生成的"})
            # 档位：**显式登记**（内核 API 会把默认档也报出来 → 直接看 permissions.json）
            want = ((d.get("interface") or {}).get("tier") or "write").lower()
            exp = AC._explicit_tier(pid)
            if exp:
                row["steps"].append({"step": "档位", "did": "已是 %s（显式登记，保持不动）" % exp})
            elif apply_:
                AC.http("/api/permissions/set", {"plugin": pid, "level": want}, "POST")
                row["steps"].append({"step": "档位", "did": "已显式登记 %s（permissions.json）" % want})
            else:
                row["steps"].append({"step": "档位", "did": "会显式登记 %s（现在只有默认档）" % want})
            if apply_:
                mrows = AC.patch_mcp_hosts(d)
                w2, p2 = AC.patch_hook_config(d)
                if mrows:
                    row["steps"].append({"step": "agent 侧 MCP",
                                         "did": "；".join("%s：%s" % (r["name"], r["did"]) for r in mrows),
                                         "where": mrows[0]["where"], "hosts": mrows})
                else:
                    row["steps"].append({"step": "agent 侧 MCP", "did": "跳过（不吃 MCP）", "where": ""})
                row["steps"].append({"step": "agent 侧 hook", "did": w2, "where": p2})
            else:
                _hs = AC.mcp_hosts(d)
                row["steps"].append({"step": "agent 侧 MCP",
                                     "did": ("会写 %d 个面：%s" % (len(_hs), "、".join(x["name"] for x in _hs)))
                                            if _hs else "跳过（不吃 MCP）",
                                     "where": _hs[0]["config"] if _hs else ""})
                row["steps"].append({"step": "agent 侧 hook", "did": "会写",
                                     "where": AC.hook_snippet(d)[1]})
            row["ok"] = True
            _stats["adds"] += 1
        except Exception as e:
            row["ok"] = False
            row["error"] = "%s: %s" % (type(e).__name__, e)
            _stats["errors"] += 1
            _stats["last_error"] = row["error"]
        out.append(row)
    return {"ok": all(x.get("ok") for x in out) if out else False, "applied": apply_, "results": out}


def tool_prompt(args=None):
    """提示词（可复制）。两种：

    · 能接的（有描述文件）→ kind="attach"：工具怎么用、剩下那半截怎么接；
    · **接不了的**（候选表命中 / 模糊发现 / 只登记没描述文件）→ kind="onboard"：接入确认单，
      问它（或它的主人）四件事：吃不吃 MCP、配置在哪、有没有 hook、怎么启动。
    """
    a = args or {}
    aid = str(a.get("id") or "")
    d = AC.load_descs().get(aid)
    if not d:
        row = AC.find_row(aid)          # 只查这一个 —— 原来调 scan_all 要等 4-5 秒（点一下就卡）
        if not row:
            return {"ok": False, "error": "认不到这个 agent：%s（先自动识别一下）" % aid}
        _stats["prompts"] += 1
        return {"ok": True, "kind": "onboard", "id": aid, "name": row.get("name") or aid,
                "level": "", "ops_count": 0, "prompt": AC.make_onboard_prompt(row)}
    ops, level = _ops_of(d["plugin_id"])
    _stats["prompts"] += 1
    return {"ok": True, "kind": "attach", "id": aid, "name": d.get("name") or aid, "level": level,
            "ops_count": len(ops), "prompt": AC.make_prompt(d, level, ops)}


def tool_verify(args=None):
    """验收一个 agent（控制台"详情"里显示的那几条）"""
    a = args or {}
    aid = str(a.get("id") or "")
    d = AC.load_descs().get(aid)
    if not d:
        return {"ok": False, "error": "没有描述文件 agents/%s.yaml" % aid}
    _stats["verifies"] += 1
    st = AC._status_of(d)
    checks = [{"name": "接口已登记", "ok": st["registered"]},
              {"name": "在跑", "ok": st["running"]},
              {"name": "档位已登记", "ok": bool(st["level"]),
               "value": "%s%s" % (st["level"], "" if AC._explicit_tier(d["plugin_id"]) else "（默认档）")},
              {"name": "agent 侧命令不弹黑窗", "ok": not st.get("console_cmds"),
               "value": "、".join(st.get("console_cmds") or []) or "pythonw"}]
    ops, _lv = _ops_of(d["plugin_id"])
    checks.append({"name": "真调一次 brain.health", "ok": bool(ops)})
    for x in (st.get("mcp_hosts") or []):        # 一个 agent 可能好几个面：每面一条
        if not x.get("exists"):
            checks.append({"name": "agent 侧 MCP：%s" % x["name"], "ok": True, "value": "没装/跳过"})
        else:
            checks.append({"name": "agent 侧 MCP：%s" % x["name"], "ok": bool(x.get("ok")),
                           "value": "在位" if x.get("ok") else "没配"})
    return {"ok": all(c["ok"] for c in checks), "id": aid, "status": st, "checks": checks,
            "ops_count": len(ops)}


def tool_status(_args=None):
    return {"stats": dict(_stats), "agents_dir": AC.AGENTS_DIR, "config": AC.CONFIG,
            "descs": sorted(AC.load_descs()), "probes": len(AC.load_probes())}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default=PLUGIN_ID)
    a = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    probe = Probe(a.id, handlers={"agents.scan": tool_scan, "agents.add": tool_add,
                                  "agents.prompt": tool_prompt, "agents.verify": tool_verify,
                                  "agents.status": tool_status},
                  stats_provider=lambda: dict(_stats), core_url=CORE_URL)
    probe.start()
    log("agent 接入插件已启动：识别 → 勾选 → 生成 → 提示词（目录 %s）" % AC.AGENTS_DIR)
    try:
        http_post("/api/bridge/plugins/register", {
            "id": a.id, "name": "Agent 接入",
            "description": "自动识别本机上的 agent（描述文件 + 候选表 + 模糊发现疑似 MCP 配置），"
                           "勾选后按模板接入（内核侧接口 + agent 侧 MCP/hook），并生成给该 agent 的提示词。",
            "capabilities": ["agents.scan", "agents.add", "agents.prompt", "agents.verify",
                             "agents.status"], "produces": [], "consumes": []})
        log(f"注册成功 {a.id}")
    except Exception as e:
        log(f"注册失败（平台未就绪，探针周期会兜住）: {e}")

    parent_pid = os.getppid()
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
