#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentctl —— **自动识别 agent → 按模板生成接口/MCP/hook → 接上并验收**（辞玖 2026-10-05）。

    python scripts/agentctl.py scan                 # 这台机器上有哪些 agent（看识别依据）
    python scripts/agentctl.py list                 # 每个 agent：装了没 / 接口在不在 / 档位 / MCP·hook 配了没
    python scripts/agentctl.py plan <id>            # 按模板算出"要写哪几处"（**不落盘**）
    python scripts/agentctl.py add <id> --apply     # 真写（每个被动过的文件都留 .bak-agentctl）
    python scripts/agentctl.py verify <id>          # 验收：接口在跑/能力齐/档位/**真调一次 op**
    python scripts/agentctl.py activate             # 走启停台重启内核，让新插件生效

"一处描述、多处生成"：唯一要手写的是 `agents/<id>.yaml`；其余（config.yaml 条目、档位、
agent 侧 MCP 配置、hook 命令）都由模板渲染。规矩见 `templates/agent/README.md`。
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
# 2026-10-05 晚：路径可被环境变量改写 —— 只给**模拟机**用（scripts/sim_foreign.py 要在
# 假机器上把 detect -> plan -> add -> verify 整条链跑一遍，不能把真 config.yaml /
# permissions.json 写脏）。不设这些变量时，一切跟原来一模一样。
AGENTS_DIR = os.environ.get("AGENTCTL_AGENTS_DIR") or os.path.join(CORE_DIR, "agents")
TPL_DIR = os.path.join(CORE_DIR, "templates", "agent")
CONFIG = os.environ.get("AGENTCTL_CONFIG") or os.path.join(CORE_DIR, "config.yaml")
PERM_FILE = os.environ.get("AGENTCTL_PERM_FILE") or os.path.join(
    CORE_DIR, "data", "plugins", "permissions.json")
HOST = os.environ.get("SB_HOST", "http://127.0.0.1:8790")
SIM = bool(os.environ.get("AGENTCTL_SIM"))     # 模拟机模式：不惊动在跑的内核
PY = sys.executable
if HERE not in sys.path:
    sys.path.insert(0, HERE)                  # 同目录模块（mcphosts）要 import 得到
import mcphosts as MH                          # noqa: E402  多格式 MCP 面：识别/读/写/校验


def pyw(path=None):
    """**不弹黑窗的 python**：跟 python.exe 同目录的 pythonw.exe。

    为什么非它不可：agent 侧那半截（MCP 服务端 / hook 客户端）是**别人 spawn** 的
    （DSH 的 node、Claude Code 的 cli），我们管不到人家的创建标志 —— 用 python.exe
    就一闪一个黑框。pythonw.exe 是 GUI 子系统，天生没有控制台，stdio 管道照样通。
    """
    p = path or PY
    if os.name == "nt" and p.lower().endswith("python.exe"):
        cand = p[: -len("python.exe")] + "pythonw.exe"
        if os.path.isfile(cand):
            return cand
    return p


def fill_paths(x):
    """把描述文件/模板里的路径占位符，按**本机识别结果**填掉（2026-10-06 辞玖：不要写死路径）。

    两个占位符：
      `{{pyw}}`  → 本机**不弹黑窗的 python**（由 sys.executable 推；见 pyw()）
      `{{core}}` → 本平台**装在哪**（由本脚本自身位置推；见 CORE_DIR）

    递归处理 dict/list/str（描述文件是嵌套结构）。认不出来的**不编**：
    还剩 `{{...}}` 就原样留着，由模板里那句"你（或主人）知道的那个路径"兜底。
    """
    vals = {"{{pyw}}": pyw(), "{{core}}": CORE_DIR}
    if isinstance(x, str):
        for k, v in vals.items():
            x = x.replace(k, str(v))
        return x
    if isinstance(x, dict):
        return {k: fill_paths(v) for k, v in x.items()}
    if isinstance(x, list):
        return [fill_paths(v) for v in x]
    return x


def nowin_cmd(cmd):
    """命令的第一个词是 python/python.exe 的，换成 pythonw.exe（不弹黑窗）"""
    if not cmd or os.name != "nt":
        return cmd
    m = re.match(r'^\s*("?)([^"\s]*python(?:\.exe)?)\1(\s.*)?$', cmd, re.I)
    if not m:
        return cmd
    head, rest = m.group(2), (m.group(3) or "")
    low = head.lower()
    if low.endswith("python.exe"):
        new = pyw(head)
    elif low == "python":
        new = pyw()
    else:
        return cmd
    return ('"%s"' % new if m.group(1) else new) + rest


def _run(argv, **kw):
    """起外部命令的统一入口：**一律不弹黑窗**（Windows 给 CREATE_NO_WINDOW）"""
    kw.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return subprocess.run(argv, **kw)


def _run(argv, **kw):
    """起外部命令的统一入口：**一律不弹黑窗**（Windows 给 CREATE_NO_WINDOW）"""
    kw.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return subprocess.run(argv, **kw)


GUARD_STATE = os.path.join(CORE_DIR, "data", "agents", "guard.json")


def load_guard_state():
    """守护开关 + 台账（守护插件与本脚本共用这一份，谁改谁落盘）"""
    try:
        with io.open(GUARD_STATE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_guard_state(st):
    d = os.path.dirname(GUARD_STATE)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with io.open(GUARD_STATE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(st, f, ensure_ascii=False, indent=2, sort_keys=True)


def guard_info(desc):
    """这个 agent 能不能守护、现在开没开（面板/清单要显示这些）"""
    g = (desc or {}).get("guard") or {}
    kind = ((g.get("probe") or {}).get("kind") or "").lower()
    if not kind or kind == "none":
        return {"available": False}
    st = (load_guard_state().get((desc or {}).get("id")) or {})
    return {"available": True, "probe": g.get("probe") or {},
            "launcher": (g.get("launcher") or {}).get("cmd") or "",
            "enabled": bool(st.get("enabled")), "alive": st.get("alive"),
            "last_start": st.get("last_start") or "", "starts": int(st.get("starts") or 0)}


def set_tier(pid, level):
    """把档位**显式**登记到 `data/plugins/permissions.json`。

    正常走内核 API（`/api/permissions/set`，由内核负责落盘与生效）；
    模拟机模式下**直接改我们那份临时文件** —— 假机器上的 plugin id 内核不认识，
    走 API 会把真文件写进一个不存在的插件（演练时绝不允许）。
    """
    if SIM:
        try:
            with io.open(PERM_FILE, encoding="utf-8") as f:
                d = json.load(f) or {}
        except Exception:
            d = {}
        d[pid] = str(level)
        _dir = os.path.dirname(PERM_FILE)
        if _dir and not os.path.isdir(_dir):
            os.makedirs(_dir)
        with io.open(PERM_FILE, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
        return 200, {"ok": True, "note": "模拟机：直接写 %s" % PERM_FILE}
    return http("/api/permissions/set", {"plugin": pid, "level": level}, "POST")


def _hook_wired(config_path, h=None):
    """这个 agent 的 hook 到底接上没有。

    ⚠️ 2026-10-05 订正：原来只认 `hook_client.py` 字样 —— reasonix 的 hook 是它自己的
    `hook_logger.py`（等价上报 + 上下文包注入 + supervisor 转发），于是列表里误报"没配"。
    现在认：描述文件里声明的客户端（hook.client 的文件名）或通用客户端 hook_client.py。
    """
    if not config_path or not os.path.isfile(config_path):
        return False
    raw = io.open(config_path, encoding="utf-8", errors="replace").read()
    want = [os.path.basename(str((h or {}).get("client") or "")) or "hook_client.py"]
    want += ["hook_client.py", "agent_plugin.py", "core-host"]
    want += [os.path.basename(p) for p in str((h or {}).get("actual_client") or "").split(";") if p]
    return any(w and (w in raw) for w in set(want))


def _explicit_tier(pid):
    """**显式**登记在 data/plugins/permissions.json 里的档位。

    为什么要直接读文件：内核 `/api/permissions` 会把**默认档**也一起报出来（看着像"登记过了"），
    2026-10-05 就是这样漏掉的 —— workbuddy 接完"有效档位 write"，但文件里根本没有它。
    """
    try:
        with io.open(PERM_FILE, encoding="utf-8") as f:
            d = json.load(f) or {}
        return d.get(pid) or ""
    except Exception:
        return ""


def _read_text(path):
    if path and os.path.isfile(path):
        try:
            return io.open(path, encoding="utf-8", errors="replace").read()
        except Exception:
            return ""
    return ""


def _is_console_py(cmd):
    """命令的第一个词是不是会弹黑窗的 python（pythonw 不算）"""
    m = re.match(r'^\s*"?([^"\s]*)"?', cmd or "")
    if not m:
        return False
    head = m.group(1).lower()
    return head == "python" or head.endswith("python.exe")


def _near_pairs(raw, needle, bads, window=4):
    """raw 里 needle 上下 window 行内，有没有出现 bads 里的字样"""
    lines = (raw or "").splitlines()
    for i, l in enumerate(lines):
        if needle not in l:
            continue
        for j in range(max(0, i - window), min(len(lines), i + window + 1)):
            if any(b in lines[j].lower() for b in bads):
                return True
    return False


def _console_cmds(desc, mcp_raw, hook_raw):
    """哪些地方还在用会弹黑窗的 python（面板/验收要据此提示）"""
    out = []
    if _is_console_py((desc.get("mcp") or {}).get("command") or ""):
        out.append("描述文件 mcp.command")
    if _is_console_py((desc.get("hook") or {}).get("command") or ""):
        out.append("描述文件 hook.command")
    if _near_pairs(mcp_raw, "agent_plugin.py", ("python.exe",)):
        out.append("现网 MCP 配置")
    if _near_pairs(hook_raw, "hook_client.py", ("python ", "python.exe")):
        out.append("现网 hook 配置")
    return out


# ── 小工具 ───────────────────────────────────────────────────
def expand(p: str) -> str:
    """~ 与 %ENV% / $ENV 都认（描述文件里写人类路径）"""
    if not p:
        return ""
    p = os.path.expandvars(os.path.expanduser(p))
    return p


def http(path, body=None, method="GET", timeout=60):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = urllib.request.Request(HOST + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")
    except Exception as e:
        return 0, {"error": "%s: %s" % (type(e).__name__, e)}


def load_descs():
    """已确定要接入的 agent（`agents/<id>.yaml`）；下划线开头的是候选表，不算描述文件"""
    import yaml
    out = {}
    for name in sorted(os.listdir(AGENTS_DIR)):
        if name.endswith(".yaml") and not name.startswith("_"):
            with open(os.path.join(AGENTS_DIR, name), encoding="utf-8") as f:
                out[name[:-5]] = yaml.safe_load(f) or {}
    return out


def load_probes():
    """"本机上可能有哪些 agent"的候选表（`agents/_probe.yaml`）"""
    import yaml
    p = os.path.join(AGENTS_DIR, "_probe.yaml")
    if not os.path.isfile(p):
        return []
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or []


def detect_markers(markers):
    """一组特征（路径/可执行）→ 命中的那些"""
    hits = []
    for m in markers or []:
        m = str(m)
        if m.startswith("exe:"):
            w = shutil.which(m[4:])
            if w:
                hits.append("可执行 " + w)
            continue
        if any(ch in m for ch in ("\\", "/")) or m.startswith("~") or m.startswith("%"):
            p = expand(m)
            if os.path.isdir(p):
                hits.append("目录 " + p)
            elif os.path.isfile(p):
                hits.append("文件 " + p)
        else:
            w = shutil.which(m)
            if w:
                hits.append("可执行 " + w)
    return hits


def _face_state(desc, h):
    """一个面**现在**的样子：{"exists", "ok", "raw", "fmt", "shape"}。

    为什么抽出来：以前"写完算不算成功"靠看返回文案的前缀（`did.startswith("写失败")`）——
    那是字符串判据，kind 一多就会自己骗自己。现在写（patch_mcp_hosts）、验收（_status_of）、
    列表（mcp_brief）**共用这一份判据**：结构化读（按格式×形状精确找我们那条）+ 文本兜底
    （形状特殊认不出来时，例：DSH cordis 的 `- id:` 列表）。
    """
    k = (h.get("kind") or "").lower()
    fmt, shape = (h.get("format") or "none"), (h.get("shape") or "none")
    if fmt == "cli" or k == "claude-cli":
        return {"exists": True, "ok": (h.get("server_name") or "brain") in claude_mcp_names(),
                "raw": _read_text(expand("~/.claude.json")), "fmt": "cli", "shape": "cli"}
    p = expand(h.get("config") or "")
    raw = _read_text(p)
    exists = bool(p) and os.path.isfile(p)
    ok = False
    if exists and fmt in ("json", "toml", "yaml") and shape not in ("none", ""):
        try:
            found, marker, _blob = MH.read_entry(p, fmt, shape, h.get("server_name") or "brain")
            ok = bool(found and marker)
        except Exception:
            ok = False
    if not ok and raw:
        # (9) 文本兜底：光看到 agent_plugin.py 不够（共享配置文件里可能只是别人的引用），
        #     还要看到"确实是给我这条命的"：--agent / server_name / agent id
        ok = ("agent_plugin.py" in raw) and (
            "--agent" in raw or (h.get("server_name") or "brain") in raw
            or (desc.get("id") or "") in raw)
    return {"exists": exists, "ok": bool(ok and exists), "raw": raw, "fmt": fmt, "shape": shape}


def _status_of(desc, comp=None, perm=None):
    """一个 agent 现在的接入状态（控制台列表要显示这些）。

    comp/perm 可由调用方传进来（scan_all 里一次取好给所有 agent 用）——
    2026-10-05 实测：每个 agent 各打两次 HTTP，4 个 agent 就是 1768ms。
    """
    pid = desc.get("plugin_id") or ("plugin." + desc.get("id", ""))
    if comp is None:
        _st, comp = http("/api/components")
    reg = next((c for c in (comp if isinstance(comp, list) else []) if c.get("id") == pid), None)
    if perm is None:
        _st2, perm = http("/api/permissions")
    # 没有显式登记时回退到全局默认档（内核默认 write）——不回退的话，新接的 agent 验收会误报"档位 ❌"
    level = (((perm.get("byPlugin") or {}).get(pid) or {}).get("level")
             or (perm.get("level") if isinstance(perm, dict) else "")) if isinstance(perm, dict) else ""
    kind = (desc.get("mcp") or {}).get("kind") or "none"
    # **按面统计**：一个 agent 可能有好几份宿主配置（Claude Code CLI 与 Desktop 是两份）
    hs, raws = [], []
    for h in mcp_hosts(desc):
        if (h.get("format") or "none") == "none" and (h.get("kind") or "").lower() in ("none", ""):
            continue
        fs = _face_state(desc, h)                  # 判据只有这一份（写/验收/列表共用）
        raws.append(fs["raw"])
        hs.append({"name": h.get("name") or "default", "kind": h.get("kind") or "",
                   "format": fs["fmt"], "shape": fs["shape"],
                   "config": h.get("config") or "", "exists": fs["exists"], "ok": fs["ok"]})
    mcp_raw = "\n".join(raws)
    mcp_ok = (all(x["ok"] for x in hs) if hs else None)
    _hook_cfg = expand((desc.get("hook") or {}).get("config") or "")
    hook_raw = _read_text(_hook_cfg)
    running = bool(reg and reg.get("status") == "running")
    hk = (desc.get("hook") or {}).get("kind") or "none"
    if kind in ("none", ""):
        connected = bool(running and (hk in ("none", "") or _hook_wired(_hook_cfg, desc.get("hook"))))
    else:
        connected = bool(running and mcp_ok)
    return {"plugin_id": pid, "registered": bool(reg), "running": running,
            "level": level or "", "mcp_kind": kind, "mcp_ok": mcp_ok, "mcp_hosts": hs,
            "connected": connected,
            "console_cmds": _console_cmds(desc, mcp_raw, hook_raw)}


_DISCOVER_CACHE = {"t": 0.0, "v": []}


def discover_unknown_cached(ttl=60):
    """模糊发现（满盘 glob，实测 ~885ms）—— 缓存 60 秒，别每次点击都重扫一遍"""
    now = time.time()
    if now - _DISCOVER_CACHE["t"] < ttl and _DISCOVER_CACHE["v"]:
        return _DISCOVER_CACHE["v"]
    v = discover_unknown()
    _DISCOVER_CACHE["t"] = now
    _DISCOVER_CACHE["v"] = v
    return v


def discover_unknown(limit=40):
    """**模糊发现**：不靠候选表，直接在本机常见位置找"疑似 agent 的 MCP 配置"。

    辞玖要的是"自动识别本机上的 agent（不仅是 dsh、codex 等等）"—— 表能列已知的，
    真·自动还得会捡**没进表**的：任何名字像 mcp 的配置，或含 `mcpServers`/`mcp_servers` 的
    JSON/TOML/YAML。捡到只报"疑似 + 证据路径"，**不猜它是什么 agent**，等人确认。
    """
    import glob as _glob
    # 2026-10-05 扩范围：原来只看 ~ 和 %APPDATA%，漏了 %LOCALAPPDATA%（很多 Electron 应用把配置放那儿），
    # 也不认 config.json 这类名字 —— 结果"装了但没进候选表"的应用一个都发现不了。
    pats = ["~/*/mcp*.json", "~/.*/mcp*.json", "~/.config/*/mcp*.json",
            "~/.config/*/*.json", "%APPDATA%/*/mcp*.json", "%APPDATA%/*/*mcp*config*.json",
            "~/*/.mcp.json", "~/.mcp.json", "%APPDATA%/*/settings.json",
            "~/.config/*/settings.json", "%APPDATA%/*/config.toml", "~/.config/*/config.toml",
            "%LOCALAPPDATA%/*/mcp*.json", "%LOCALAPPDATA%/*/*mcp*.json",
            "%LOCALAPPDATA%/*/settings.json", "%LOCALAPPDATA%/*/config.json",
            "%APPDATA%/*/config.json", "%APPDATA%/*/*/config.json",
            "%LOCALAPPDATA%/*/*/mcp*.json", "%LOCALAPPDATA%/*/*/settings.json"]
    known_paths = set()
    for d in load_descs().values():
        p = ((d.get("mcp") or {}).get("config") or "")
        if p:
            known_paths.add(os.path.normcase(expand(p)))
    out, seen = [], set()
    for pat in pats:
        for p in _glob.glob(expand(pat)):
            key = os.path.normcase(p)
            if key in seen or key in known_paths or not os.path.isfile(p):
                continue
            seen.add(key)
            try:
                if os.path.getsize(p) > 512 * 1024:        # 大文件跳过（状态文件）
                    continue
                raw = io.open(p, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            if ("mcpServers" in raw) or ("mcp_servers" in raw) or ("context_servers" in raw):
                out.append({"path": p, "bytes": len(raw),
                            "hint": ("mcpServers" if "mcpServers" in raw else
                                     ("mcp_servers" if "mcp_servers" in raw else "context_servers"))})
            if len(out) >= limit:
                return out
    return out


def find_row(aid):
    """**只找一个** agent 的那一行（点「接入手工单」用）。

    为什么不复用 scan_all：它要跑全量（每个 agent 两次 HTTP + 满盘 glob），实测 4.4 秒；
    而"点了某个 agent 要它的提示词"只需要那一个 —— 候选表命中这条路 27ms 就够了。
    """
    d = (load_descs() or {}).get(aid)
    if d:
        ok, hits = detected(d)
        return {"id": aid, "name": d.get("name") or aid, "has_desc": True, "detected": ok,
                "evidence": hits, "status": _status_of(d), "connected": False,
                "guard": guard_info(d), "mcp_kind": (d.get("mcp") or {}).get("kind") or "none",
                "hook_kind": (d.get("hook") or {}).get("kind") or "none"}
    for pr in load_probes():
        if pr.get("id") != aid:
            continue
        hits = detect_markers(pr.get("markers") or [])
        if hits:
            return {"id": aid, "name": pr.get("name") or aid, "has_desc": False, "detected": True,
                    "evidence": hits, "status": None, "mcp_kind": pr.get("mcp") or "none",
                    "hook_kind": "none",
                    "probe": {"config_hint": pr.get("config_hint") or "", "note": pr.get("note") or "",
                              "config_file": pr.get("config_file") or ""}}
    if aid in (registered_agents() or {}):
        pid = registered_agents()[aid]
        return {"id": aid, "name": aid + "（缺描述文件）", "has_desc": False, "detected": False,
                "registered_only": True, "connected": True,
                "evidence": ["config.yaml 里已经登记成 agent 接口（%s）" % pid],
                "status": {"plugin_id": pid, "registered": True, "running": True,
                           "mcp_kind": "none", "mcp_ok": None, "connected": True,
                           "console_cmds": [], "mcp_hosts": []},
                "mcp_kind": "none", "hook_kind": "none"}
    if aid.startswith("unknown:"):
        for u in discover_unknown_cached():
            if "unknown:" + os.path.basename(u["path"]) == aid:
                return {"id": aid, "name": "（没进表）" + u["path"], "has_desc": False,
                        "detected": True, "unknown": True,
                        "evidence": ["疑似 %s 配置 %s" % (u["hint"], u["path"])],
                        "status": None, "mcp_kind": "mcp-json", "hook_kind": "none",
                        "probe": {"config_hint": u["path"], "note": "没进候选表；确认是哪个 agent 后，写一份 agents/<id>.yaml 再接入"}}
    return None


def scan_all():
    """**自动识别本机上的 agent**：描述文件里的 + 候选表里命中的（含还没描述文件的）。

    返回 [{id, name, has_desc, detected, evidence, status?, probe?}] —— 这就是控制台那份列表。
    """
    out, seen = [], set()
    _st, _comp = http("/api/components")           # 一次取好，给所有 agent 用（原来每个各打两次）
    _st2, _perm = http("/api/permissions")
    for aid, d in load_descs().items():
        ok, hits = detected(d)
        seen.add(aid)
        st = _status_of(d, _comp, _perm)
        # 2026-10-06：config.yaml 里有这条接口、内核组件表里却没有它 → **内核还没读到**
        # （清单是启动时快照；点「接入并生成」只写文件）。面板据此显示「⏳ 待热加载」，
        # 免得看起来像"没接上"。热加载走 POST /api/plugins/reload（不用重启内核）。
        _pend = bool(config_has(d.get("plugin_id") or "") and not st.get("registered"))
        out.append({"id": aid, "name": d.get("name") or aid, "has_desc": True,
                    "detected": ok, "evidence": hits, "status": st,
                    "pending_restart": _pend,
                    "connected": bool(st.get("connected")), "guard": guard_info(d),
                    "mcp_kind": (d.get("mcp") or {}).get("kind") or "none",
                    "hook_kind": (d.get("hook") or {}).get("kind") or "none"})
    for pr in load_probes():
        aid = pr.get("id")
        if not aid or aid in seen:
            continue
        hits = detect_markers(pr.get("markers") or [])
        if hits:
            out.append({"id": aid, "name": pr.get("name") or aid, "has_desc": False,
                        "detected": True, "evidence": hits, "status": None,
                        "mcp_kind": pr.get("mcp") or "none", "hook_kind": "none",
                        "probe": {"config_hint": pr.get("config_hint") or "",
                                  "note": pr.get("note") or "",
                                  "config_file": pr.get("config_file") or ""}})
    # **模糊发现**：没进表的疑似 MCP 配置也一并报出来（只报"疑似 + 证据"，不猜是什么 agent）
    try:
        for u in discover_unknown_cached():
            out.append({"id": "unknown:" + os.path.basename(u["path"]),
                        "name": "（没进表）" + u["path"], "has_desc": False, "detected": True,
                        "evidence": ["疑似 %s 配置 %s" % (u["hint"], u["path"])], "status": None,
                        "mcp_kind": "mcp-json", "hook_kind": "none", "unknown": True,
                        "probe": {"config_hint": u["path"],
                                  "note": "没进候选表；确认是哪个 agent 后，写一份 agents/<id>.yaml 再接入"}})
    except Exception as _e:
        pass
    # **已经接上的必须有显示**：config.yaml 里登记成 agent 接口、但没有描述文件的，也列出来
    try:
        _st, comp = http("/api/components")
        comps = {c.get("id"): c for c in (comp if isinstance(comp, list) else [])}
        for aid, pid in registered_agents().items():
            if aid in seen:
                continue
            seen.add(aid)
            reg = comps.get(pid) or {}
            out.append({"id": aid, "name": aid + "（缺描述文件）", "has_desc": False,
                        "detected": False, "registered_only": True, "connected": True,
                        "evidence": ["config.yaml 里已经登记成 agent 接口（%s）" % pid],
                        "status": {"plugin_id": pid, "registered": True,
                                   "running": reg.get("status") == "running", "level": "",
                                   "mcp_kind": "none", "mcp_ok": None, "connected": True,
                                   "console_cmds": []},
                        "mcp_kind": "none", "hook_kind": "none"})
    except Exception:
        pass
    return out


def registered_agents():
    """config.yaml 里已经登记成"agent 接口"的（args 里带 `--agent <id>`）。

    为什么要它：**已经连接上的必须有显示** —— 哪怕描述文件被删了、或本机那份目录没认出来，
    只要接口还开着，控制台就该看得见它。
    """
    out = {}
    raw = _read_text(CONFIG)
    if not raw:
        return out
    try:
        import yaml
        cfg = yaml.safe_load(raw) or {}
    except Exception:
        return out
    for pl in (cfg.get("plugins") or []):
        pid = pl.get("id") or ""
        args = [str(x) for x in (pl.get("args") or [])]
        aid = ""
        for i, a in enumerate(args):
            if a == "--agent" and i + 1 < len(args):
                aid = args[i + 1]
        if aid and "agent_plugin.py" in " ".join(args):
            out[aid] = pid
    return out


def console_risks():
    """哪里还在用会弹黑窗的 python（验收/体检用）"""
    out = []
    for aid, d in sorted(load_descs().items()):
        for w in (_status_of(d).get("console_cmds") or []):
            out.append({"id": aid, "where": w})
    return out


def _fill_tpl(text, mapping):
    """把模板里的 {{key}} 换掉（跟 templates/agent 其他 tpl 一个写法）"""
    for k, v in mapping.items():
        text = text.replace("{{%s}}" % k, str(v))
    return text


def onboard_ctx(row):
    """给「接入手工单」准备**真值**（辞玖：「一次给完整、无误」——不留占位符让它猜）。

    能填的都填上：你自己的检测路径、最像配置文件的那个路径、你大概该走哪条分支、证据全文。
    """
    aid = row.get("id") or ""
    name = row.get("name") or aid
    st = row.get("status") or {}
    pr = row.get("probe") or {}
    ev = [str(x) for x in (row.get("evidence") or [])]
    paths, files, exes, cfg, raw = [], [], [], "", []
    for x in ev:
        raw.append(x)
        p, tag_hit = "", ""
        for tag in ("目录 ", "文件 ", "可执行 "):
            if x.startswith(tag):
                p, tag_hit = x[len(tag):].strip(), tag.strip()
                break
        if not p:
            continue
        # (2) 按**证据标签**归类（原来一律塞 paths，把 .py 文件也当目录了）
        if tag_hit == "目录":
            paths.append(p)
        elif tag_hit == "文件":
            files.append(p)
        elif tag_hit == "可执行":
            exes.append(p)
        else:
            paths.append(p)
        if p.lower().endswith((".json", ".toml", ".yaml", ".yml")) and not cfg:
            cfg = p
    if pr.get("config_file"):
        # 候选表明确写了 MCP 配置是哪个文件 → **以它为准**（别让启发式在好几个 .toml/.json 里猜错）
        cfg = expand(str(pr["config_file"]))
    if not cfg:                       # 证据里没有明确文件：从"疑似配置"那句里捡一个路径
        for x in ev:
            for tok in x.replace("（", " ").replace("）", " ").split():
                if tok.lower().endswith((".json", ".toml")):
                    cfg = tok
                    break
            if cfg:
                break
    kind = row.get("mcp_kind") or "mcp-json"
    if (kind or "").lower() in ("none", ""):
        # 不吃 MCP 的 agent（例：reasonix）：证据里那些 .toml/.json 是**它自己的应用配置**，
        # 不是 MCP 客户端配置 —— 不清掉的话会被误判进"分支 A"（2026-10-05 踩过）。
        cfg = ""
    note, hint = (pr.get("note") or ""), (pr.get("config_hint") or "")
    # 只看**证据**（路径/文件）—— 备注里提到"hook"不算它有 hook（Coze 就这么被误判过一次）
    has_hook = any(("hook" in s.lower() or "settings.json" in s.lower()) for s in raw)
    if cfg:
        guess = "分支 A（有 MCP 配置文件，按 A1/A2 做）"
        why = "我们在 `%s` 看到了配置文件" % cfg
    elif has_hook:
        guess = "分支 B（没找到 MCP 配置，但像是有 hooks）"
        why = "证据里出现了 hooks/settings 的痕迹"
    else:
        guess = "分支 C（没看到可写的 MCP 配置，也没看到 hook）"
        why = "只有目录，没有任何可写的 MCP 配置文件"
    if row.get("unknown"):
        # 模糊发现出来的：这份配置**不知道属于谁** → 先让它认领一个 id，别拿 unknown:xxx 去建文件
        aid2 = "<你的 id>"
        return {"id": aid2, "name": name, "agent_id": "agent." + aid2, "plugin_id": "plugin." + aid2,
                "paths": paths, "config": cfg, "mcp_kind": kind, "note": note, "hint": hint,
                "raw": raw, "branch_guess": "分支 A（有配置文件），但**先认领 id**",
                "branch_why": why,
                "id_note": ("⚠️ 这份是从 **%s** 发现的 —— 我们**不知道它属于谁**。\n"
                            "第一步：把 id 定下来（你是什么 agent 就用那个名字），本单里所有 `<你的 id>` "
                            "（含文件名 `agents/<你的 id>.yaml`）都换成它。" % cfg)}
    # (3) 有 hook 证据就预填（写死 none 会把"已经有事件面"的 agent 填错）
    hook_note = ("它已经有 hooks（证据里看到 hook/settings）—— "
                 "填你**实际的形状**，别照拄 none"
                 if has_hook else "有 hook 才填；没有就保持 none")
    return {"id": aid, "name": name,
            "agent_id": row.get("agent_id") or st.get("agent_id") or ("agent." + aid),
            "plugin_id": row.get("plugin_id") or st.get("plugin_id") or ("plugin." + aid),
            "paths": paths, "files": files, "exes": exes, "config": cfg, "mcp_kind": kind,
            "hook_note": hook_note, "events_hint": ("hook" if has_hook else "none"),
            "note": note, "hint": hint, "raw": raw, "branch_guess": guess, "branch_why": why}


def _format_matrix():
    """「格式 × 形状」对照表（接入手工单 A1.5 用）。

    从 `mcphosts` 那份真值生成，**不手抄** —— 手抄的表跟代码一飘，接单的人就照错了形状。
    """
    ref = (("json", "mcpServers", "Claude Desktop / Cursor / Cline / WorkBuddy 这类"),
           ("toml", "mcp_servers", "Codex CLI"),
           ("toml", "plugins_list", "Reasonix（**我们只读不写**：用它自己的 CLI）"),
           ("yaml", "mcpServers", "DSH(cordis) / 部分 YAML 系宿主"),
           ("yaml", "context_servers", "Zed / Continue"),
           ("yaml", "plugins_list",
            "DSH(cordis.patch.yml)：顶层数组 / `- insert:` 列表里的 `- id:` 条目"))
    return "\n".join("| `%s` | `%s` | %s |" % (f, MH.key_for(f, s), who) for f, s, who in ref)


def make_onboard_prompt(row):
    """**接不了的那种** agent 的提示词 —— 一份「接入手工单」（辞玖 2026-10-05）。

    · 头一版写成"问你四件事" → 辞玖：应该把**内核这边的规矩和模板**整包给它，**接入由它全权负责**；
    · 二版又要求"回来找人" → 辞玖：它可能**根本回不来**，所以要**一次给完整、无误**：
      自带三十秒自测分岔、每步给确切命令与失败对照、失败把证据写进固定文件、并备一段直接转发给主人的话。
    文案在 `templates/agent/handoff.tpl`（改文案改那个文件，别改代码）；填好的例子见
    `templates/agent/examples/coze-handoff.md`。
    """
    c = onboard_ctx(row)
    try:
        tpl_text = tpl("handoff.tpl")
    except Exception as e:                 # 模板没了就**大声说**，别静默给半份
        return ("# 接入手工单：%s（%s）\n\n"
                "⚠️ 内核这边的模板 `templates/agent/handoff.tpl` 读不到（%s）—— 先修它，再生成这份单子。\n"
                % (c["name"], c["id"], e))
    m = dict(c)

    def _txt(v):
        # 注入到 YAML/正文里的文本也要消毒：反斜杠换正斜杠、双引号换单引号
        # （不然 unknown: 那种 name 是 `（没进表）C:\Users\…`，塞进 description: "…" 就是非法 YAML）
        return str(v if v is not None else "").replace("\\", "/").replace('"', "'")

    m["id"] = _txt(c.get("id"))
    m["name"] = _txt(c.get("name") or c.get("id"))
    m["agent_id"] = _txt(c.get("agent_id"))
    m["plugin_id"] = _txt(c.get("plugin_id"))

    # 路径一律**按本机识别结果**填（2026-10-06 辞玖：不要写死路径）——
    # 模板里写 {{pyw}} / {{core}}…，这里给真值；对方机器上跑 agentctl 时自然是它自己的值。
    _pl = CORE_DIR.replace("\\", "/")
    _ap = os.path.join(CORE_DIR, "plugins", "agent_plugin.py").replace("\\", "/")
    _hc = os.path.join(CORE_DIR, "plugins", "hook_client.py").replace("\\", "/")
    m["pyw"] = pyw()
    m["pyw_slash"] = pyw().replace("\\", "/")
    m["platform"] = CORE_DIR
    m["platform_slash"] = _pl
    m["agent_plugin"] = _ap
    m["agent_plugin_slash"] = _ap
    m["hook_client"] = _hc
    m["hook_client_slash"] = _hc

    def _y(p):
        # (1) 一律交**正斜杠**：Windows 路径原样塞进 YAML 双引号里，反斜杠 U 那种是非法转义
        #     （reasonix 照抄 A1 直接 yaml 解析失败，报给我了）
        return ('"%s"' % str(p).replace("\\", "/")) if p else ""

    m["detect_paths"] = ", ".join(_y(p) for p in (c.get("paths") or []))
    m["detect_files"] = ", ".join(_y(p) for p in (c.get("files") or []))
    m["detect_exes"] = ", ".join(_y(p) for p in (c.get("exes") or []))
    m["hook_note"] = c.get("hook_note") or "有 hook 才填；没有就保持 none"
    m["events_hint"] = c.get("events_hint") or "none"
    m["evidence"] = (("- 我们看到的：\n" + "\n".join("  - " + x for x in c["raw"]))
                     if c["raw"] else "- 我们没看到明确证据")
    m["hint"] = ("- 配置线索：%s" % c["hint"]) if c["hint"] else ""
    m["note"] = ("- 备注：%s" % c["note"]) if c["note"] else ""
    m["branch_note"] = "- 我们为什么这么判断：%s" % c["branch_why"]
    m["config_probe"] = _y(c["config"])[1:-1] if c["config"] else "你（或主人）知道的那个 MCP 配置文件路径"
    m["branch_guess"] = c["branch_guess"]
    m["config_path"] = (c["config"] or "").replace("\\", "/")
    # (6) "先建空壳"要**按格式来**（JSON 空壳 / TOML 空表 / YAML 空键各不相同）；
    #     形状认不出来就不建空壳，改走宿主自己的官方 CLI。
    _cp = expand(c["config"]) if c.get("config") else ""
    _fmt, _shape, _wr, _how = MH.face_of(c.get("mcp_kind"), "", "", _cp)
    if _cp and os.path.isfile(_cp):
        _g, _hits = MH.detect_shapes(_cp)
        if _g in ("json", "toml", "yaml"):
            _fmt = _g
        if _hits:
            _shape = _hits[0][0]
    m["own_format"] = "%s x %s" % (_fmt, _shape)
    # 我们**不知道**它配置文件在哪时（Coze 这种），别渲染出空的 `` —— 换成一句指路的话
    m["config_where"] = (m["config_path"] or "你宿主里那个 MCP 配置文件")
    # 命令与路径**按本机识别结果**给（2026-10-06）：pythonw 由 sys.executable 推、平台根由脚本位置推
    _cmd = pyw()
    _ar = [os.path.join(CORE_DIR, "plugins", "agent_plugin.py"), "--agent", c["id"], "--mcp"]
    if _how == "unknown":
        m["own_snippet"] = ("（你这一面的形状我们认不出来 —— **别照抄 JSON**，"
                            "用你自己宿主的官方 CLI 注册，服务名 `brain`）")
        m["shell_step"] = ("你的面认不出形状（kind=%s）—— **别去建空壳**：优先用你自己宿主的官方 CLI"
                           "（例：reasonix-cli.exe mcp add brain -- ...）" % (c.get("mcp_kind") or "?"))
    elif not _wr:
        # 只读形状（例：Reasonix 的 `[[plugins]]` 就是 External MCP）—— 我们**故意不写**，
        # 所以绝不能叫它去建空壳/手改那个文件（2026-10-05 上一版就是这么错的，被自测逮住）。
        m["own_snippet"] = ("（这个形状我们**只读不写** —— 用你自己宿主的官方 CLI 注册，服务名 `brain`）")
        m["shell_step"] = ("你这个形状我们**只读不写**（kind=%s）—— **别去建空壳、也别手改那个文件**："
                           "用你自己宿主的官方 CLI（例：reasonix-cli.exe mcp add brain -- ...）"
                           % (c.get("mcp_kind") or "?"))
    elif (c.get("mcp_kind") or "").lower() == "dsh-plugin":
        # DSH 是 cordis 的 `- id:` 列表（不是普通 mcp 表）—— 形状表里那格解释不了它
        m["own_snippet"] = ("- id: mcp-%s\n  name: '@deepseek-ai/dsh-mcp-client'\n  config:\n"
                            "    serverName: brain\n    transport: stdio\n"
                            "    command: '%s'\n    args: %s\n    cwd: '%s'"
                            % (c["id"], _cmd, json.dumps(_ar, ensure_ascii=False),
                               CORE_DIR.replace("\\", "/")))
        m["shell_step"] = "（DSH 那种 `- id:` 列表不用建空壳：往现成的 `plugins:` 里加一条）"
    elif _fmt in ("json", "toml", "yaml") and _shape not in ("none", ""):
        _key = MH.key_for(_fmt, _shape)
        _shell = {"json": '{"%s": {}}' % _key, "toml": '[%s]' % _key,
                  "yaml": '%s: {}' % _key}[_fmt]
        m["own_snippet"] = MH.snippet(_fmt, _shape, "brain", _cmd, _ar, CORE_DIR)
        _place = ("`%s`" % m["config_path"]) if m["config_path"] else "你自己宿主认的那个位置"
        m["shell_step"] = ("你的 MCP 配置文件如果还不存在，先建一个**跟你格式一致**的空壳："
                           "`%s`（放%s）；已经有的话**别动它别的条目**，只往 `%s` 里加我们这一条。"
                           % (_shell, _place, _key))
    else:                                   # 兜底：判不出格式/形状就**什么都别写**
        m["own_snippet"] = "（判不出你这一面的形状 —— 用你自己宿主的官方 CLI 注册，服务名 `brain`）"
        m["shell_step"] = ("判不出你这一面的形状（kind=%s, format x shape=%s）—— "
                           "**别去建空壳**，用你自己宿主的官方 CLI"
                           % (c.get("mcp_kind") or "?", m.get("own_format") or "?"))
    m["format_matrix"] = _format_matrix()
    m["id_note"] = c.get("id_note") or ""
    return _fill_tpl(tpl_text, m)


def make_prompt(desc, level="", ops=None):
    """生成**给那个 agent 的提示词**（复制给 agent 自己看，让它把剩下那半截接上）。

    ops：该 agent 现在放开的操作 id 列表（控制台/插件从 /api/ops 取；不给就只讲通用规矩）。
    """
    aid, pid = desc.get("id"), desc.get("plugin_id")
    name = desc.get("name") or aid
    m = desc.get("mcp") or {}
    h = desc.get("hook") or {}
    L = ["你是 %s。现在你被接入「枢阁」（一个本机知识库平台），平台里你的身份是 `%s`。" % (name, pid),
         ""]
    L += ["## 你手上会多出来一个工具",
          "- 工具名 `brain_ops`（经 MCP；在支持原生工具名的宿主里可能显示成 `mcp__brain__brain_ops`）",
          '- 调用形状：`{"op": "<操作 id>", "args": {…}}`；`op` 用 `list` 可以列出你现在能调什么',
          '- 例：`{"op": "brain.search", "args": {"q": "关键词", "limit": 8}}`、'
          '`{"op": "brain.read", "args": {"path": "项目/xxx.md"}}`',
          '- 自检：调一次 `{"op": "brain.health", "args": {}}`，应返回库的文件数/块数统计',
          ""]
    if ops:
        L += ["## 你现在放开的操作（%s 档，共 %d 条）" % (level or "?", len(ops)),
              "`" + "`、`".join(sorted(ops)) + "`", ""]
    L += ["## 规矩（平台侧定的，请遵守）",
          "- **库是真相源**：改库里的东西就走工具；不要直接改盘上的文件（那会造成库与盘漂移）",
          "- 读之前先看：`brain.note` / `brain.blocks` / `brain.article` 拿结构，`brain.search` 先找",
          "- 改动尽量**追加/按小节改**（`brain.append` / `brain.section`），少整篇重写",
          "- 删除/覆盖/可见性这类操作，先跟主人说一声再做", ""]
    _faces = mcp_hosts(desc)
    if _faces:
        # 按面逐个给片段：片段与"真正落盘的东西"同源（_snippet_for -> mcphosts.snippet），
        # 所以照抄不会抄错形状（以前是按 kind 手抄，kind 一多就开始飘）。
        L += ["## 如果你那边还没有这个工具（按**你自己的面**接；一个 agent 可能有好几个面）"]
        for _f in _faces:
            _what, _where, _txt = _snippet_for(desc, _f)
            if not _txt:
                L += ["- **%s**：%s" % (_f["name"], _what)]
                continue
            L += ["- **%s** —— %s%s" % (_f["name"], _what,
                                       ("（落点 `%s`）" % _where) if _where else ""),
                  "```", _txt.rstrip("\n"), "```"]
    if (h.get("kind") or "none") not in ("none", ""):
        L += ["- 事件（把你这边的动作报给平台）：hook 命令指到 `%s`"
              % (h.get("command") or h.get("client") or "plugins/hook_client.py")]
    L += ["", "接好之后，主人会在控制台「Agent 接入」里点「详情 → 验收」，看到你这一条全绿。"]
    return "\n".join(L)

def detected(desc: dict):
    """识别：路径 / 文件 / 可执行，任一命中。返回 (是否认到, 命中的依据)"""
    d = desc.get("detect") or {}
    hits = []
    for p in d.get("paths") or []:
        if os.path.isdir(expand(p)):
            hits.append("目录 " + expand(p))
    for p in d.get("files") or []:
        if os.path.isfile(expand(p)):
            hits.append("文件 " + expand(p))
    for x in d.get("exes") or []:
        w = shutil.which(x)
        if w:
            hits.append("可执行 " + w)
    return bool(hits), hits


def tpl_args_line(desc):
    """模板里那行 args（config.yaml 与 agent 侧配置都用它比对"是不是模板生成的"）"""
    for ln in render_plugin_entry(desc).split("\n"):
        if ln.strip().startswith("args:"):
            return ln.strip()
    return ""


def replace_in_block(path, is_start, is_target, new_line):
    """在"以 is_start 开头的那一块"里，把 is_target 那一行换成 new_line。

    返回 True=改了 / False=没找到（调用方据此决定要不要新建）。
    **就地改行**，不重排文件、注释全留。
    """
    lines = io.open(path, encoding="utf-8").read().split("\n")
    start = next((i for i, l in enumerate(lines) if is_start(l)), None)
    if start is None:
        return False
    end = len(lines)
    for i in range(start + 1, len(lines)):
        l = lines[i]
        if l.strip() and not l.startswith((" ", "\t", "#")):        # 顶层键
            end = i
            break
        if l.startswith("  - id: ") and i > start:                   # 下一个列表项
            end = i
            break
    for i in range(start + 1, end):
        if is_target(lines[i]):
            if lines[i].rstrip() == new_line.rstrip():
                return False                                          # 已经就是模板值
            lines[i] = new_line
            backup(path)
            io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(lines))
            return True
    return False


def backup(path):
    if os.path.isfile(path):
        bak = path + ".bak-agentctl"
        shutil.copy2(path, bak)
        return bak
    return ""


def tpl(name):
    with open(os.path.join(TPL_DIR, name), encoding="utf-8") as f:
        return f.read()


_CLAUDE_MCP_CACHE = {"t": 0.0, "v": set()}


def claude_mcp_names(fresh=False, ttl=30):
    """`claude mcp list` 里的服务器名（Claude Code 的 MCP 状态以它自己的 CLI 为准）。

    ⚠️ 这一步要**起一次 claude CLI**（实测 1~1.5 秒）—— 所以默认缓存 30 秒；
    "写配置"那条路必须拿新鲜的（否则"已存在就不 remove"会写失败），调用时传 fresh=True。
    """
    now = time.time()
    if not fresh and (now - _CLAUDE_MCP_CACHE["t"] < ttl) and _CLAUDE_MCP_CACHE["v"]:
        return set(_CLAUDE_MCP_CACHE["v"])
    cl = shutil.which("claude")
    if not cl:
        return set()
    try:
        r = _run([cl, "mcp", "list"], capture_output=True, text=True, encoding="utf-8",
                 errors="replace", timeout=120)
        out = (r.stdout or "") + (r.stderr or "")
        names = {ln.split(":", 1)[0].strip() for ln in out.splitlines()
                 if ":" in ln and not ln.startswith(("Checking", "Usage"))}
        _CLAUDE_MCP_CACHE["t"], _CLAUDE_MCP_CACHE["v"] = time.time(), set(names)
        return names
    except Exception:
        return set()


def render_plugin_entry(desc):
    body = tpl("plugin-entry.yaml.tpl")
    # 模板头部那两行是我们自己的说明（含"变量：…"），渲染时剥掉，只留要落盘的那段
    body = "\n".join([x for x in body.split("\n")
                       if not x.lstrip().startswith(("# 变量：", "# 内核侧 agent 接口插件条目"))])
    for k, v in (("{{id}}", desc["id"]), ("{{agent_id}}", desc["agent_id"]),
                 ("{{plugin_id}}", desc["plugin_id"]), ("{{name}}", desc.get("name") or desc["id"]),
                 ("{{capabilities_csv}}", ",".join((desc.get("interface") or {}).get("capabilities") or []))):
        body = body.replace(k, str(v))
    return body.rstrip("\n") + "\n"


def mcp_hosts(desc):
    """一个 agent 的**多个面**（宿主各读各的配置文件）。

    为什么：同一个 agent 常常有好几个"面" —— Claude Code 的命令行与 Claude Desktop 是**两份配置**，
    接了 CLI 不会自动接 GUI。新写法：
        mcp:
          kind: claude-cli                    # 默认接法（每个面可以覆盖）
          server_name / command / args / cwd / transport
          hosts:
            - {name: claude-code-cli, kind: claude-cli}
            - {name: claude-desktop, kind: mcp-json, config: "%APPDATA%/Claude/claude_desktop_config.json"}
    老写法（只有单个 mcp.config）自动当成一个面 `default` —— 老描述文件不用改。

    2026-10-05 晚加**多格式**：每个面可以**不写 kind**，直接声明格式与形状：
        hosts:
          - {name: zed,   format: yaml, shape: context_servers, config: "%APPDATA%/Zed/settings.json"}
          - {name: codex, format: toml, shape: mcp_servers,     config: "~/.codex/config.toml"}
    优先级：显式 format/shape > 老 kind 查表 > **看文件自己认**（认不出就 none，不硬猜）。
    判出来的 (format, shape, writable, how) 挂在这个面的字典上 —— 写、验收、列表都用它，
    别再各判各的（判据只有 `scripts/mcphosts.py` 那一份）。
    """
    m = desc.get("mcp") or {}
    # ⚠️ 描述文件里可以写 `{{pyw}}` / `{{core}}`（见 fill_paths）——  在**这一刻**按本机识别结果填，
    #    这样"描述文件"是通用的，写进对方配置的却是这台机器的真路径。
    base = {"kind": m.get("kind") or "none", "server_name": m.get("server_name") or "brain",
            "transport": m.get("transport") or "stdio",
            "command": fill_paths(m.get("command") or "{{pyw}}"),
            "args": fill_paths(list(m.get("args") or [])), "cwd": fill_paths(m.get("cwd") or "{{core}}"),
            "config": m.get("config") or "", "entry_id": m.get("entry_id") or "",
            "format": m.get("format") or "", "shape": m.get("shape") or "",
            "table": m.get("table") or "", "readonly": m.get("readonly")}
    hs = m.get("hosts") or []
    out = []
    if not hs:
        # ⚠️ 2026-10-05 晚（模拟器逮到的）：这里以前是 "kind 是 none/空 就 return []" ——
        # 于是"只写了 format/shape、没写 kind"的面全被丢掉（add 说"跳过（不吃 MCP）"）。
        # 新规矩：**明确写了 `kind: none`** 才算"作者说它不吃 MCP"；
        #          **没写 kind** 的，允许"看文件自己认"（认不出才不给面）。
        if str(m.get("kind") or "").strip().lower() == "none":
            return []
        h = dict(base)
        h["name"] = "default"
        _f, _s, _w, _how = MH.face_of(h.get("kind"), h.get("format"), h.get("shape"),
                                      expand(h.get("config") or ""), h.get("readonly"))
        if _s == "none" and not (base["format"] or base["shape"]):
            return []                      # 没写 kind、文件里也没有 MCP 表 → 真的不吃
        out.append(h)
    else:
        for i, h in enumerate(hs):
            x = dict(base)
            x.update({k: v for k, v in (h or {}).items() if v not in (None, "")})
            x["name"] = (h or {}).get("name") or ("host%d" % (i + 1))
            out.append(x)
    for x in out:
        _fmt, _shape, _wr, _how = MH.face_of(x.get("kind"), x.get("format"), x.get("shape"),
                                             expand(x.get("config") or ""), x.get("readonly"))
        x["format"], x["shape"], x["writable"], x["how"] = _fmt, _shape, _wr, _how
    return out


def _snippet_for(desc, h):
    """按**一个面**的接法生成片段（返回 (说明, 落点, 片段文本)）"""
    kind, path = (h.get("kind") or "none"), expand(h.get("config") or "")
    # ⚠️ 判据要跟 mcp_hosts 一致：**kind 空 且 format 也空**才算"不吃 MCP"。
    # 只写了 format/shape 的面（新写法）在这里被早退过一次 —— 模拟器第二轮逮到的。
    if kind in ("none", "") and (h.get("format") or "none") in ("none", ""):
        return "不吃 MCP（跳过）", "", ""
    cmd = pyw(h.get("command") or PY)
    args = list(h.get("args") or [])
    cwd = h.get("cwd") or CORE_DIR
    sname = h.get("server_name") or "brain"
    if kind == "mcp-json":
        txt = json.dumps({"mcpServers": {sname: {"command": cmd, "args": args, "cwd": cwd}}},
                         ensure_ascii=False, indent=2)
        return "mcpServers.%s（JSON）" % sname, path, txt
    if kind == "codex-toml":
        arr = "[" + ", ".join('"%s"' % a for a in args) + "]"
        txt = ('\n[mcp_servers.%s]\ncommand = "%s"\nargs = %s\ncwd = "%s"\n'
               % (sname, cmd.replace("\\", "\\\\"), arr, cwd.replace("\\", "\\\\")))
        return "mcp_servers.%s（TOML）" % sname, path, txt
    if kind == "claude-cli":
        line = " ".join(["claude", "mcp", "add", sname, "--", cmd] + args)
        return "claude mcp add（走它自己的 CLI 注册）", "", line + "\n"
    if kind == "reasonix-toml":
        # Reasonix 的 [[plugins]]（TOML，不是 mcpServers）——**我们不写它**（写坏了会把它已有的十几条服务冲掉），
        # 由 Reasonix 自己的官方 CLI 写：reasonix-cli.exe mcp add brain -- …
        line = "reasonix-cli.exe mcp add %s -- %s %s" % (sname, cmd, " ".join(args))
        return "[[plugins]]（这个形状我们只读不写 —— 用它的官方 CLI）", path, line + "\n"
    if kind == "dsh-plugin":
        arr = "[" + ", ".join("'%s'" % a for a in args) + "]"
        txt = ("\n- id: %s\n  name: '@deepseek-ai/dsh-mcp-client'\n  config:\n"
               "    serverName: %s\n    transport: %s\n    command: '%s'\n    args: %s\n"
               "    cwd: '%s'\n    toolCallTimeoutMs: 180000\n    failOnStartupError: false\n"
               % (h.get("entry_id") or ("mcp-" + desc["id"]), sname,
                  h.get("transport") or "stdio", cmd, arr, cwd))
        return "DSH 的 mcp-%s（cordis.patch.yml）" % desc["id"], path, txt
    # ── 新写法：不依赖 kind，任何「格式 × 形状」都能出片段（片段与落盘同源：mcphosts.snippet）──
    fmt, shape = (h.get("format") or "none"), (h.get("shape") or "none")
    if fmt in ("json", "toml", "yaml") and shape not in ("none", ""):
        if not h.get("writable", True):
            return ("%s（这个形状我们**只读不写** —— 用它自己的官方 CLI）" % shape, path,
                    MH.snippet("cli", "cli", sname, cmd, args, cwd) + "\n")
        return ("%s.%s（%s）" % (shape, sname, fmt.upper()), path,
                MH.snippet(fmt, shape, sname, cmd, args, cwd, h.get("table")) + "\n")
    if fmt == "cli":
        return ("走它自己的 CLI 注册（要手接）", path,
                MH.snippet("cli", "cli", sname, cmd, args, cwd) + "\n")
    return "这种 kind 还没实现自动写：%s" % (kind or "（没写 kind/format）"), path, ""


def mcp_snippet(desc):
    """老接口：第一个面的片段（多处还在用，保持兼容）"""
    hs = mcp_hosts(desc)
    if not hs:
        return "不吃 MCP（跳过）", "", ""
    return _snippet_for(desc, hs[0])


def mcp_brief(desc):
    """cmd_list 里那一格：`2/2 面` / `1/2 面（claude-desktop 没装）` / `没配` / `—`"""
    hs = mcp_hosts(desc)
    if not hs:
        return "—"
    try:
        st = _status_of(desc).get("mcp_hosts") or []
    except Exception:
        st = []
    if not st:
        return "没配"
    ok = sum(1 for x in st if x.get("ok"))
    miss = [x["name"] for x in st if not x.get("ok")]
    return "%d/%d 面%s" % (ok, len(st), ("（%s）" % "、".join(miss)) if miss else "")


def hook_snippet(desc):
    h = desc.get("hook") or {}
    kind, path = h.get("kind") or "none", expand(h.get("config") or "")
    if kind in ("none", ""):
        return "不吃 hook（跳过）", "", ""
    cmd = nowin_cmd(h.get("command")) or ("%s %s/plugins/hook_client.py --agent %s"
                                          % (pyw(), CORE_DIR, desc["id"]))
    if kind == "claude-settings":
        txt = ('把 hooks 里的命令改成：\n      %s\n' % cmd)
        return "hooks.*.command（settings.json）", path, txt
    if kind == "reasonix-toml":
        return "hook 客户端（现在由 hook_logger.py 承担）", path, "命令：%s\n" % cmd
    return "模板里没有这种 kind：%s" % kind, path, ""


def config_has(plugin_id):
    """config.yaml 里有没有这个插件条目。

    ⚠️ 2026-10-05 栽过：写成 `"- id: x" in f`（f 是文件对象）—— 那是对**文件按行迭代**、
    比的是"整行相等"，带缩进的条目永远比不中 → 每跑一次 add 就多插一条（真插出过 5 份重复）。
    """
    with open(CONFIG, encoding="utf-8") as f:
        return ("- id: %s" % plugin_id) in f.read()


def insert_plugin_entry(plugin_id, entry):
    """把条目插到 config.yaml 的 plugins: 列表末尾（**文本级插入**，不重排整个 YAML，注释全留）"""
    with open(CONFIG, encoding="utf-8") as f:
        lines = f.readlines()
    start = next((i for i, l in enumerate(lines) if l.rstrip() == "plugins:"), None)
    if start is None:
        raise SystemExit("config.yaml 里找不到 plugins:（结构变了？）")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        l = lines[i]
        if l.strip() and not l.startswith((" ", "\t", "#")):     # 第一个顶层键 = 列表结束
            end = i
            break
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1                                            # 收掉尾部空行再插
    lines[insert_at:insert_at] = ["\n" + entry]
    backup(CONFIG)
    with open(CONFIG, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(lines)
    return CONFIG


def _patch_one_host(desc, h):
    """把一个面写进去（返回 (结果文本, 落点)）"""
    kind = h.get("kind") or "none"
    what, path, txt = _snippet_for(desc, h)
    if kind == "claude-cli":
        name = h.get("server_name") or "brain"
        cl = shutil.which("claude")
        if not cl:
            return "没找到 claude 命令（要手接）", ""
        if name in claude_mcp_names(fresh=True):      # 写配置要新鲜的（缓存可能过期）
            # 已登记：**也要把命令对齐成 pythonw** —— 原来"已在就跳过"，旧的 python.exe 一直留着闪黑框
            _run([cl, "mcp", "remove", name], capture_output=True, text=True, encoding="utf-8",
                 errors="replace", timeout=60, cwd=h.get("cwd") or CORE_DIR)
        argv = [cl, "mcp", "add", name, "--", pyw(h.get("command") or PY)] + list(h.get("args") or [])
        r = _run(argv, capture_output=True, text=True, encoding="utf-8",
                 errors="replace", timeout=120, cwd=h.get("cwd") or CORE_DIR)
        return ("写入" if r.returncode == 0 else "写失败：%s" % (r.stderr or r.stdout or "")[:80],
                " ".join(argv))
    if kind in ("none", "") and (h.get("format") or "none") in ("none", ""):
        return "跳过（这个面不吃 MCP）", ""
    if kind == "reasonix-toml":
        # 只验不写（写它的 TOML 有风险：可能把它已有的 [[plugins]] 条目冲掉）
        raw = io.open(path, encoding="utf-8").read() if os.path.isfile(path) else ""
        if raw and ((h.get("server_name") or "brain") in raw) and ("agent_plugin.py" in raw):
            return "已在（这个形状我们只读不写）", path
        return "要接请用它的官方 CLI：%s" % _snippet_for(desc, h)[2].strip(), path
    if not path:
        return "这个面没写 config（要手接）", ""
    if not os.path.isfile(path):
        return "没找到配置文件（没装？）", path
    # ⚠️ 2026-10-05 晚：原来 mcp-json / codex-toml 各有**一段手写落盘**（自己 dumps、自己追加）。
    # 同一件事两套实现，坏文件时报的话都不一样（模拟器里 brokenapp 报的是"读不出 JSON"，
    # 而不是 mcphosts 那句更准的"本来就解析不过、我们不动它"）。现在统一走 mcphosts.write_entry：
    # 备份 -> 写 -> 回读校验 -> 别人的条目丢没丢 -> 不过就回滚。特殊形状（CLI / cordis / 只读）才留分支。
    if kind == "dsh-plugin":
        entry_id = h.get("entry_id") or ("mcp-" + desc["id"])
        raw = io.open(path, encoding="utf-8").read()
        if ("- id: %s" % entry_id) in raw:
            # **已存在也要对齐模板**：① args 指到 agent_plugin.py ② command 用 pythonw（不弹黑窗）
            # ⚠️ 替换出来的行要**保持原来那一行的缩进**（原来写死 4 空格：跟"按缩进插入"的结果对不上，
            #    第二次 add 又会改一次文件 → 幂等破功。2026-10-05 模拟器第三轮逮到）。
            lines = raw.split("\n")
            i0 = next((i for i, l in enumerate(lines) if l.strip() == "- id: %s" % entry_id), None)
            if i0 is None:
                return "这条在文件里定位不到（要手接）", path
            j1 = next((j for j in range(i0 + 1, len(lines))
                       if lines[j].strip().startswith("- id:")), len(lines))
            # ⚠️ `command` / `args` 是 **config: 的子键**（dsh-mcp-client 从 config 里读它们），
            #    所以缩进 = `config:` 那行的缩进 + 2 —— **不是** `- id:` 的缩进 + 2。
            #    2026-10-05 晚：旧代码写死 4 空格，把 `- insert:` 里那条写成了 config 的同级，
            #    于是现网 `~/.dsh/profiles/web/cordis.patch.yml` **整份解析不过**（DSH 重启就会丢工具面）。
            ci = next((i for i in range(i0, min(i0 + 16, len(lines)))
                       if lines[i].strip().startswith("config:")), None)
            if ci is None:
                return "已在（这条不是模板生成的形状，没动它）", path
            sub_ind = lines[ci][:len(lines[ci]) - len(lines[ci].lstrip())] + "  "
            # ⚠️ cwd 也要对齐（2026-10-06 迁移时漏了：目录改名后这条还指着旧路径，
            #    宿主拉起 MCP 服务端就会拿一个不存在的 cwd）
            want = [("command", "command: '%s'" % pyw(h.get("command") or PY)),
                    ("args", "args: [" + ", ".join("'%s'" % x
                                                   for x in (h.get("args") or [])) + "]"),
                    ("cwd", "cwd: '%s'" % (h.get("cwd") or CORE_DIR))]
            changed = 0
            for k, txt2 in want:
                i = next((i for i in range(i0, j1) if lines[i].strip().startswith(k + ":")), None)
                if i is None:                        # 少了就补在这条末尾
                    lines.insert(j1, sub_ind + txt2)
                    j1 += 1
                    changed += 1
                elif lines[i] != sub_ind + txt2:
                    lines[i] = sub_ind + txt2
                    changed += 1
            if changed:
                bak = MH._backup(path)
                io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(lines))
                if not MH.parse_ok(path, "yaml"):     # 写完立刻回读：坏了就回滚（今晚就是缺这一步）
                    if bak:
                        shutil.copy2(bak, path)
                    else:
                        io.open(path, "w", encoding="utf-8", newline="\n").write(raw)
                    return "写完 YAML 解析不过 → 已回滚", path
                return "已按模板更新（args + command 挪到 config 下面 + pythonw）", path
            return "已在（跳过）", path
        # ⚠️ DSH 的 cordis.patch.yml 是**顶层数组**：每个元素一条 loader patch
        #    （`- id: xxx` 改某条 / `- insert: [...]` 往列表里插），**没有 `plugins:` 这个键**。
        #    所以：① 有现成的 `- insert:` 列表 → 插到那个列表末尾，缩进跟里面条目一致；
        #          ② 没有 → 新起一条 `- insert:`（列 0），把条目缩进 4 格塞进去。
        lines = raw.split("\n")
        ins = next((i for i, l in enumerate(lines) if l.strip() == "- insert:"), None)
        if ins is None:
            lines += ["", "- insert:"] + [((("    ") + x) if x.strip() else x)
                                          for x in txt.strip("\n").split("\n")]
        else:
            # 块边界：**缩进不比 `- insert:` 深**就是到头了（不能见到 `- ` 就往下吞 ——
            # 顶层还有 `- id: system-prompt` 这类条目，吞进去插入点就跑到文件尾、写出非法 YAML）。
            base = lines[ins][:len(lines[ins]) - len(lines[ins].lstrip())]
            indent4, end = base + "    ", ins + 1
            while end < len(lines):
                l = lines[end]
                if not l.strip():
                    end += 1
                    continue
                lead = l[:len(l) - len(l.lstrip())]
                if len(lead) <= len(base):
                    break
                if l.lstrip().startswith("- "):
                    indent4 = lead
                end += 1
            lines[end:end] = [((indent4 + x) if x.strip() else x)
                              for x in txt.strip("\n").split("\n")]
        new = "\n".join(lines)
        if not new.endswith("\n"):
            new += "\n"
        bak = MH._backup(path)
        io.open(path, "w", encoding="utf-8", newline="\n").write(new)
        if not MH.parse_ok(path, "yaml"):        # 写完立刻回读；不过就回滚（坏文件绝不留下）
            if bak:
                shutil.copy2(bak, path)
            else:
                io.open(path, "w", encoding="utf-8", newline="\n").write(raw)
            return "写完 YAML 解析不过 → 已回滚（缩进对不上？）", path
        return "追加", path
    # ── 新写法：按 格式 × 形状 通用落盘（备份 / 写 / 回读校验 / 失败回滚都在 mcphosts 里）──
    fmt, shape = (h.get("format") or "none"), (h.get("shape") or "none")
    if fmt in ("json", "toml", "yaml") and shape not in ("none", ""):
        sname2 = h.get("server_name") or "brain"
        cmd2, args2 = pyw(h.get("command") or PY), list(h.get("args") or [])
        cwd2 = h.get("cwd") or CORE_DIR
        if not path:
            return "这个面没写 config（要手接）", ""
        if not os.path.isfile(path):
            return "没找到配置文件（没装？）", path
        if not h.get("writable", True):
            found, marker, _blob = MH.read_entry(path, fmt, shape, sname2)
            if found and marker:
                return "已在（这个形状我们只读不写）", path
            return ("这个形状我们**只读不写** —— 用它的官方 CLI：%s"
                    % MH.snippet("cli", "cli", sname2, cmd2, args2, cwd2).strip()), path
        ok2, did2, _where2 = MH.write_entry(path, fmt, shape, sname2, cmd2, args2, cwd2,
                                            table=h.get("table"))
        return ("写入" if (ok2 and did2 in ("写入", "追加")) else did2), path
    return "这种 kind 还没实现自动写（要手接）", path


def patch_mcp_hosts(desc):
    """**按面逐个写**（一个 agent 可能有好几份宿主配置）。返回 [{name, kind, config, ok, did, where}]"""
    out = []
    for h in mcp_hosts(desc):
        did, where = _patch_one_host(desc, h)
        fs = _face_state(desc, h)          # ok = **写完读一遍**，不看文案前缀
        out.append({"name": h["name"], "kind": h.get("kind") or "",
                    "format": h.get("format") or "", "shape": h.get("shape") or "",
                    "config": h.get("config") or "", "ok": fs["ok"], "did": did, "where": where})
    return out


def patch_agent_config(desc):
    """老接口：返回 (按面汇总的文本, 第一个落点)"""
    rows = patch_mcp_hosts(desc)
    if not rows:
        return "跳过（不吃 MCP）", ""
    return "；".join("%s：%s" % (r["name"], r["did"]) for r in rows), rows[0]["where"]


def patch_hook_config(desc):
    h = desc.get("hook") or {}
    kind = h.get("kind") or "none"
    if kind != "claude-settings":
        return "跳过", ""
    path = expand(h.get("config") or "")
    cmd = h.get("command") or ""
    if not os.path.isfile(path):
        return "没找到配置文件", path
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    want = nowin_cmd(cmd) if cmd else ""
    if "hook_client.py" in raw:
        # 已有：只把"那一条命令"对齐成 pythonw（别的 hook 一律不碰）
        cur = ""
        for line in raw.splitlines():
            if "hook_client.py" in line:
                mm = re.search(r'"command"\s*:\s*"([^"]*)"', line)
                if mm:
                    cur = mm.group(1)
        if not want or not cur or cur.replace("\\\\", "\\") == want:
            return "已在（跳过）", path
        try:
            json.loads(raw)
        except Exception as e:
            return "现有 settings.json 不合法（没动）：%s" % str(e)[:60], path
        new = raw.replace('"command": "%s"' % cur,
                          '"command": "%s"' % want.replace("\\", "\\\\"))
        if new == raw:
            return "命令形状不认得（要手接）：%s" % cur, path
        backup(path)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
        return "已把 hook 命令换成 pythonw", path
    # 认的是"当年那版模板写的旧命令"（用户名不参与匹配：只认这段相对路径）
    old = "python " + os.path.join(os.path.expanduser("~"), ".claude", "logs", "hook_log.py").replace("\\", "/")
    if old not in raw:
        return "现有 hook 命令与模板不符（要手接）", path
    backup(path)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(raw.replace(old, cmd))
    return "改命令", path


# ── 命令 ─────────────────────────────────────────────────────
def cmd_scan(descs, _a):
    print("这台机器上识别到的 agent：\n")
    seen_ids = set()
    for aid, d in descs.items():
        seen_ids.add(aid)
        ok, hits = detected(d)
        _s = _status_of(d)
        _mark = ("🔗 已接入" if _s.get("connected")
                 else ("接口在跑，agent 侧还没接上" if _s.get("running") else ""))
        print("  %s %-14s %-22s %s" % ("✅" if ok else "·", aid, (d.get("name") or ""), _mark))
        for h in hits:
            print("        ← %s" % h)
        if not ok:
            print("        （没认到：detect 里的 paths/files/exes 都没命中）")

    # 候选表命中的：认到了、但还没写描述文件（面板里就是"认到但没描述文件"）
    pr_hits = []
    for pr in load_probes():
        aid = pr.get("id")
        if not aid or aid in seen_ids:
            continue
        hits = detect_markers(pr.get("markers") or [])
        if hits:
            pr_hits.append((aid, pr, hits))
    if pr_hits:
        print("\n认到、但还没描述文件的（候选表 agents/_probe.yaml）：")
        for aid, pr, hits in pr_hits:
            print("  · %-14s %s" % (aid, pr.get("name") or ""))
            for h in hits:
                print("        ← %s" % h)
            if pr.get("note"):
                print("        %s" % pr["note"])

    # 模糊发现：没进表、但像 MCP 配置的（不猜是哪个 agent，等人确认）
    try:
        us = discover_unknown()
    except Exception:
        us = []
    if us:
        print("\n没进表、但像 MCP 配置的（要人确认是哪个 agent）：")
        for u in us:
            print("  · %s（%s）" % (u["path"], u["hint"]))
    return 0


def cmd_guard(descs, a):
    """守护开关（跟守护插件共用 data/agents/guard.json；插件在跑就下一轮生效）"""
    aid = a.id
    d = descs.get(aid)
    if not d:
        print("没有这个 agent 的描述文件：agents/%s.yaml" % aid)
        return 2
    info = guard_info(d)
    if not info.get("available"):
        print("%s 没声明 guard —— 要在 agents/%s.yaml 里写一段 guard: {probe, launcher, kill} 才能守护" % (aid, aid))
        print("（有现成例子：agents/dsh.yaml 的 guard 段）")
        return 2
    if a.action == "status":
        print("守护 %s：%s ｜ 探针 %s ｜ %s ｜ 拉起 %d 次%s" % (
            aid, "开" if info["enabled"] else "关", info.get("probe"),
            "在跑" if info.get("alive") else ("没在跑" if info.get("alive") is False else "还没探过"),
            info["starts"], (" ｜ 上次拉起 " + info["last_start"]) if info.get("last_start") else ""))
        return 0
    st = load_guard_state()
    row = dict(st.get(aid) or {})
    row["enabled"] = (a.action == "on")
    row["miss"] = 0
    st[aid] = row
    save_guard_state(st)
    print("守护 %s → %s（开关 %s）" % (aid, "开" if row["enabled"] else "关", GUARD_STATE))
    return 0


def cmd_list(descs, _a):
    st, comp = http("/api/components")
    plugins = {c.get("id"): c for c in (comp if isinstance(comp, list) else [])}
    st2, perm = http("/api/permissions")
    blocks = (perm.get("byPlugin") or {}) if isinstance(perm, dict) else {}
    levels = {pid: (b or {}).get("level") for pid, b in blocks.items()}
    registered = set(perm.get("plugins") or []) if isinstance(perm, dict) else set()
    print("%-13s %-6s %-9s %-8s %-7s %-10s %s" % ("agent", "装了", "接口", "在跑", "档位", "MCP 配置", "hook 配置"))
    for aid, d in descs.items():
        det, _ = detected(d)
        pid = d.get("plugin_id")
        reg = plugins.get(pid) or {}
        running = (reg.get("status") == "running")
        what, path, _txt = mcp_snippet(d)
        mcp_ok = mcp_brief(d)          # 按面：2/2 面 / 1/2 面（claude-desktop 没装）/ —
        h = d.get("hook") or {}
        hp = expand(h.get("config") or "")
        hook_ok = "—" if (h.get("kind") or "none") == "none" else (
            ("在" if _hook_wired(hp, h) else "没配"))
        print("%-13s %-6s %-9s %-8s %-7s %-10s %s" % (
            aid, "✅" if det else "·", "✅" if (pid in plugins or pid in registered) else "·",
            "✅" if running else "·", levels.get(pid) or "-", mcp_ok, hook_ok))
    print("\n（接口 = 平台组件表里有没有它；在跑 = 内核 Supervisor 报的 status）")
    return 0


def cmd_plan(descs, a):
    for aid in a.ids:
        d = descs[aid]
        det, hits = detected(d)
        print("═" * 74)
        print("agent：%s（%s）  识别：%s" % (aid, d.get("name") or "", "✅ 认到" if det else "· 没认到"))
        for h in hits:
            print("    ← %s" % h)
        print("\n① 内核侧接口 → config.yaml")
        if config_has(d["plugin_id"]):
            print("   （已有 - id: %s，跳过）" % d["plugin_id"])
        else:
            print("   " + render_plugin_entry(d).replace("\n", "\n   ").rstrip())
        print("\n② 档位 → data/plugins/permissions.json")
        st, pd = http("/api/permissions?plugin=" + d["plugin_id"])
        print("   现在：%s（没登记就默认 write）" % (pd.get("level") or "未登记"))
        print("\n③ agent 侧 MCP（一个 agent 可能有好几个面，各读各的配置）")
        _hs = mcp_hosts(d)
        if not _hs:
            print("   （不吃 MCP）")
        for _h in _hs:
            _what, _path, _txt = _snippet_for(d, _h)
            print("   · %-20s %s → %s" % (_h["name"], _what, _path or "（走它自己的 CLI 注册）"))
        print("\n④ agent 侧 hook → %s" % (hook_snippet(d)[1] or "（不吃 hook）"))
        print("   " + hook_snippet(d)[0])
    print("═" * 74)
    print("（上面是 plan，不落盘。要真写：agentctl add %s --apply）" % " ".join(a.ids))
    return 0


def reload_kernel():
    """让内核**重读 config.yaml**，把新加的插件热启起来 —— 不用重启内核、不动在跑的接口。

    对应内核的 `POST /api/plugins/reload`（2026-10-06 加，起因：agent 接入写完 config.yaml 后
    必须重启整个内核才生效，把所有 agent 接口一起打断 3~5 秒）。
    返回 (ok, result)；老内核没这个接口时 ok=False，调用方退回到 activate。
    """
    try:
        req = urllib.request.Request(CORE_URL + "/api/plugins/reload", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            d = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        return bool(d.get("ok")), (d.get("result") or d)
    except Exception as e:
        return False, {"error": "%s: %s" % (type(e).__name__, e)}


def cmd_reload(_descs, _a):
    """重读插件清单，把**新加**的插件热启起来（不重启内核）。"""
    if not _core_alive():
        print("⚠️ 内核没在跑 —— 先 activate（或点控制台「⟳ 重启内核」）")
        return 1
    ok, r = reload_kernel()
    if not ok:
        print("⚠️ 热加载没成：%s" % (r.get("error") or r))
        print("     老内核没有 /api/plugins/reload 时就会这样 —— 退路还是 activate（重启内核）")
        return 1
    print("已重读 config.yaml（内核没重启，在跑的接口没动）：")
    print("   新起：%s" % ("、".join(r.get("started") or []) or "（无）"))
    print("   原有：%s 个" % len(r.get("unchanged") or []))
    if r.get("errors"):
        print("   ❌ 起不来：%s" % json.dumps(r["errors"], ensure_ascii=False))
        return 1
    return 0


def cmd_add(descs, a):
    for aid in a.ids:
        d = descs[aid]
        print("── %s（%s）" % (aid, d.get("name") or ""))
        want_args = tpl_args_line(d)
        if not config_has(d["plugin_id"]):
            if a.apply:
                p = insert_plugin_entry(d["plugin_id"], render_plugin_entry(d))
                print("   ① config.yaml：已插入内核侧接口（备份 %s）" % (p + ".bak-agentctl"))
            else:
                print("   ① config.yaml：**会插入**内核侧接口（干跑）")
        elif a.apply and replace_in_block(
                CONFIG, lambda l: l.strip() == "- id: %s" % d["plugin_id"],
                lambda l: l.strip().startswith("args:"), "    " + want_args):
            print("   ① config.yaml：**已按模板更新**（原来是老跑法）")
        else:
            print("   ① config.yaml：已是模板生成的，跳过")
        # ② 档位：**显式登记**（内核 API 会把默认档也报出来，所以看文件说了算）
        want = ((d.get("interface") or {}).get("tier") or "write").lower()
        exp = _explicit_tier(d["plugin_id"])
        if exp:
            print("   ② 档位：已是 %s（显式登记，保持不动）" % exp)
        elif a.apply:
            st2, _pd = set_tier(d["plugin_id"], want)
            print("   ② 档位：已显式登记为 %s（写进 data/plugins/permissions.json，%s）" % (want, st2))
        else:
            print("   ② 档位：**会显式登记**为 %s（干跑；现在只有默认档）" % want)
        if a.apply:
            w1, p1 = patch_agent_config(d)
            print("   ③ MCP 配置：%s %s" % (w1, p1))
            w2, p2 = patch_hook_config(d)
            print("   ④ hook 配置：%s %s" % (w2, p2))
        else:
            print("   ③ MCP 配置：**会写** %s（干跑）" % (mcp_snippet(d)[1] or "（不吃 MCP）"))
            print("   ④ hook 配置：**会写** %s（干跑）" % (hook_snippet(d)[1] or "（不吃 hook）"))
    if a.apply and not SIM and not os.environ.get("AGENTCTL_NO_RELOAD"):
        # 热加载：内核重读 config.yaml，只起新条目 —— **不用重启内核**（也就不会打断别的 agent）
        print("\n⑤ 热加载（内核重读 config.yaml；不重启内核、不动在跑的接口）…")
        ok, r = reload_kernel()
        if ok:
            print("   已起：%s" % ("、".join(r.get("started") or []) or "（无新条目）"))
            if r.get("errors"):
                print("   ⚠️ 有起不来的：%s（看 data/logs/core.log）" % json.dumps(r["errors"], ensure_ascii=False))
            print("   注：agent 侧那半截仍要**重启那个 agent** 才会加载新的 MCP 条目（宿主的事，我们替不了）")
        else:
            print("   ⚠️ 热加载没成（%s）—— 退路：python scripts\\agentctl.py activate" % (r.get("error") or r))
    elif a.apply:
        print("\n（自测/模拟机模式：跳过热加载；真跑要生效就 python scripts\\agentctl.py reload）")
    if a.apply:
        print("      然后：python scripts/agentctl.py verify %s" % " ".join(a.ids))
    return 0


def cmd_verify(descs, a):
    bad = 0
    for aid in a.ids:
        d = descs[aid]
        pid = d["plugin_id"]
        print("── 验收 %s（%s）" % (aid, pid))
        st, comp = http("/api/components")
        reg = next((c for c in (comp if isinstance(comp, list) else []) if c.get("id") == pid), None)
        print("   %s 接口已登记" % ("✅" if reg else "❌"))
        bad += 0 if reg else 1
        if reg:
            print("   %s 在跑（status=%s）" % ("✅" if reg.get("status") == "running" else "❌",
                                            reg.get("status")))
            bad += 0 if reg.get("status") == "running" else 1
            caps = set(reg.get("capabilities") or [])
            # 注意：**投递（agent.deliver）不是能力名** —— 它靠注册里的 deliverTool 字段路由，
            # 不出现在 capabilities 里（2026-10-05 这里报过一次假红）
            need = {"agent.bridge", "agent.record.%s" % aid, "agent.attach.%s" % aid,
                    "agent.detach.%s" % aid}
            miss = sorted(need - caps)
            print("   %s 能力齐（缺：%s）" % ("✅" if not miss else "❌", miss or "无"))
            bad += 0 if not miss else 1
        st, pd = http("/api/permissions?plugin=" + pid)
        lv = (pd.get("level") or ((pd.get("byPlugin") or {}).get(pid) or {}).get("level") or "")
        exp = _explicit_tier(pid)
        print("   %s 档位：%s%s" % ("✅" if lv else "❌", lv or "未登记",
                                  "" if exp else "（默认档，没显式登记 —— `add --apply` 会补上）"))
        bad += 0 if lv else 1
        st, r = http("/api/ops/call", {"plugin": pid, "op": "brain.health", "args": {}}, "POST")
        ok = st == 200 and (r.get("ok") is not False)
        print("   %s 真调一次 brain.health（%s）" % ("✅" if ok else "❌", st))
        bad += 0 if ok else 1
        # **每个面各一行**（一个 agent 可能 CLI / GUI 两个面）
        for x in (_status_of(d).get("mcp_hosts") or []):
            where = x["config"] or "走它自己的 CLI 注册"
            if not x["exists"]:
                print("   ➖ agent 侧 MCP：%s（没装/跳过）%s" % (x["name"], where))
            elif x["ok"]:
                print("   ✅ agent 侧 MCP：%s（在位）%s" % (x["name"], where))
            else:
                print("   ❌ agent 侧 MCP：%s（没配）%s" % (x["name"], where))
                bad += 1
    print("\n%s" % ("全部通过" if not bad else "有 %d 条不过" % bad))
    return 0 if not bad else 1


CORE_URL = os.environ.get("SB_CORE", "http://127.0.0.1:8795")      # 内核自己的口（8790 是宿主）


def _core_alive():
    """内核在不在 —— **问内核自己的 /healthz**（8795）。

    2026-10-05 栽过：问 8790 的 /api/health（那是宿主/库的口径），结果把"内核在跑"判成没跑，
    于是 activate 直接 start（空操作）→ 新配置压根没被加载，还以为重启过了。
    """
    try:
        r = urllib.request.Request(CORE_URL + "/healthz")
        with urllib.request.urlopen(r, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _core_pids():
    """内核进程还在不在 —— 优先用 psutil（**不起子进程**，也就不会踩中文控制台的解码坑）"""
    try:
        import psutil
    except Exception:
        return None
    out = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if (p.info.get("name") or "").lower() == "core.exe":
                out.append(int(p.info["pid"]))
        except Exception:
            continue
    return out


def _core_running():
    """判活：psutil 优先；没有就用 tasklist，**显式给 encoding**（别再用 locale 解）"""
    pids = _core_pids()
    if pids is not None:
        return bool(pids)
    r = _run(["tasklist", "/FI", "IMAGENAME eq core.exe"], capture_output=True,
             text=True, encoding="utf-8", errors="replace")
    return "core.exe" in (r.stdout or "")


def cmd_activate(_descs, _a):
    """停 → **起** → 等就绪。**start 无论如何都会试，而且不止试一次。**

    四个教训（都留在这儿，别再犯）：
      1. 2026-10-05 第一版**只 stop 忘了 start** → 内核躺那儿；
      2. 同一天：停完在"等进程消失"时崩了 —— `subprocess.run(text=True)` 没给 encoding，
         中文控制台下按 locale 解码 tasklist 的中文输出 → UnicodeDecodeError，start 整个没跑到；
      3. 所以判活走 psutil（不起子进程）、停那一段的错误全接住（连 Ctrl+C 也算），start 一定要跑；
      4. 但**光跑一次还不够**：如果"等停干净"那步挂了，我们可能在旧内核还没退干净时就发 start，
         那一次等于白发 —— 所以 start + 等就绪**重试 3 轮**，每轮最多等 10 秒。
    """
    err = ""
    try:
        if not _core_alive():
            print("内核本来就没在跑 —— 直接 start")
        else:
            print("① 走启停台停内核（/api/core/stop）…")
            http("/api/core/stop", {}, "POST", timeout=60)
            gone = False
            for _ in range(60):
                time.sleep(0.5)
                if not _core_running():
                    gone = True
                    break
            print("   已停" if gone else "   ⚠️ 30 秒还没停干净，仍然尝试 start")
    except BaseException as e:                 # 连 Ctrl+C 也算：内核不能停在半路
        err = "%s: %s" % (type(e).__name__, e)
        print("   ⚠️ 停内核这一段出错了（%s）—— 照样往下走：start 必须试" % err)

    for attempt in range(1, 4):
        print("② start（/api/core/start）…" if attempt == 1 else "② start 重试 %d/3…" % attempt)
        try:
            st, d = http("/api/core/start", {}, "POST", timeout=60)
            if st == 200:
                print("   已受理：%s" % (d.get("note") or d.get("action") or ""))
            else:
                # 宿主重启窗口里这条可能被 connection reset —— **不据此判失败**，
                # 以"内核起来没有"为准（2026-10-05：这里误报过一次）
                print("   start 没拿到干净回执（%s）—— 继续等内核，起来了就算成功"
                      % str(d.get("error") or st)[:80])
        except Exception as e:
            print("   start 请求本身出错（%s: %s）—— 继续等" % (type(e).__name__, e))
        for _ in range(20):                    # 每轮最多等 10 秒
            time.sleep(0.5)
            if _core_alive():
                try:
                    with urllib.request.urlopen(urllib.request.Request(CORE_URL + "/healthz"),
                                                timeout=5) as resp:
                        dd = json.loads(resp.read().decode("utf-8", "replace") or "{}")
                except Exception:
                    dd = {}
                print("✅ 内核回来了（pid=%s，%s 个进程）" % (dd.get("pid"), dd.get("processes")))
                return 0
        if attempt < 3:
            print("   这轮没起来 —— 3 秒后再来（旧内核可能还没退干净）")
            time.sleep(3)

    print("⚠️ 试了 3 轮还没就绪 —— 内核可能还躺着（不会自己好）。补一条就行：")
    print("     再跑一次：python scripts\\agentctl.py activate")
    print("     （它会看到内核没在跑 → 直接 start；这就是补救动作）")
    print("     或点控制台的「⟳ 重启内核」。还不行看 data/logs/core.log")
    if err:
        print("   （停内核那一步的原始错误：%s）" % err)
    return 1


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("scan", "list", "activate", "reload"):
        sub.add_parser(name)
    pg = sub.add_parser("guard")
    pg.add_argument("id")
    pg.add_argument("action", nargs="?", default="status", choices=["on", "off", "status"])
    for name in ("plan", "add", "verify"):
        p = sub.add_parser(name)
        p.add_argument("ids", nargs="+")
        if name == "add":
            p.add_argument("--apply", action="store_true", help="真写（默认干跑）")
    a = ap.parse_args()
    descs = load_descs()
    if a.cmd in ("plan", "add", "verify"):
        miss = [x for x in a.ids if x not in descs]
        if miss:
            print("没有这些 agent 的描述文件：%s（现有：%s）" % (miss, ", ".join(descs)))
            return 2
    return {"scan": cmd_scan, "list": cmd_list, "plan": cmd_plan, "guard": cmd_guard,
            "add": cmd_add, "verify": cmd_verify, "activate": cmd_activate,
            "reload": cmd_reload}[a.cmd](descs, a)


if __name__ == "__main__":
    sys.exit(main())
