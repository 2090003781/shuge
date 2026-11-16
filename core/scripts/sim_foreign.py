#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**外机模拟器**：在一台"假电脑"上把「多格式 MCP 面」整条链真跑一遍。

辞玖 2026-10-05 晚：「配置文件要考虑 toml、config、yaml 等多格式…而且**在别人电脑上不会有信给你**」。
所以这条自测不是"单元测试 mcphosts"，而是**端到端**：拿一份假 HOME/APPDATA，
造 10 种不同形状的宿主配置（有的带注释、有的带别人的条目、有的是坏的、有的只读），
然后真跑 `agentctl.py plan / add --apply / add --apply / verify`，
按"真机上会长成什么样"逐条验收。

跑法：`python scripts\sim_foreign.py`（不需要内核认得这些假 agent；内核那几条会 ❌ 是**预期**）
它**不碰**真的 `config.yaml` / `permissions.json`（跑完比对哈希，变了就报错）。
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import mcphosts as MH                                  # noqa: E402

SIMROOT = os.path.join(CORE, "data", "tmp", "foreign-sim")
FAKE_HOME = os.path.join(SIMROOT, "home")
FAKE_APPDATA = os.path.join(SIMROOT, "appdata")
FAKE_LOCAL = os.path.join(SIMROOT, "localappdata")
FAKE_AGENTS = os.path.join(SIMROOT, "agents")
FAKE_CONFIG = os.path.join(SIMROOT, "config.yaml")
FAKE_PERM = os.path.join(SIMROOT, "data", "plugins", "permissions.json")
REAL_CONFIG = os.path.join(CORE, "config.yaml")
REAL_PERM = os.path.join(CORE, "data", "plugins", "permissions.json")

FAILS = []
NCHK = 0


def check(name, cond, detail=""):
    global NCHK
    NCHK += 1
    if cond:
        print("  PASS %s" % name)
    else:
        print("  FAIL %s%s" % (name, ("  <- %s" % str(detail)[:200]) if detail else ""))
        FAILS.append(name)


def sha(p):
    try:
        return hashlib.sha256(io.open(p, "rb").read()).hexdigest()
    except Exception:
        return ""


def posix(p):
    return str(p).replace("\\", "/")


def pyw():
    exe = sys.executable
    if os.name == "nt" and exe.lower().endswith("python.exe"):
        c = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.isfile(c):
            return c
    return exe


def rel(p):
    """假机器上的路径：home 底下的用 ~，appdata 底下的用 %APPDATA%（描述文件里人写的那种）"""
    pp = posix(p)
    if pp.lower().startswith(posix(FAKE_APPDATA).lower()):
        return "%APPDATA%" + pp[len(posix(FAKE_APPDATA)):]
    if pp.lower().startswith(posix(FAKE_HOME).lower()):
        return "~" + pp[len(posix(FAKE_HOME)):]
    return pp


# ── 10 种形状（都是真机上真实存在的那种长相）────────────────────────
CODE_CFG = """# 我的 codex 配置（这行注释不许被吃掉）
model = "gpt-5"

[mcp_servers.other]
command = "node"
args = ["other.js"]
"""

REASONIX_CFG = """# reasonix 的 External MCP servers（我们**只读不写**这一份）
[[plugins]]
name = "mimo-vision"
command = "mimo.exe"
args = []

[[plugins]]
name = "obsidian"
command = "obs.exe"
args = []
"""

CONTINUE_CFG = """# continue 的配置
name: my-assistant
version: 1.0.0

mcpServers:
  other-one:
    command: node
    args: ["other.js"]
"""

THEME_CFG = """# 只有主题，没有任何 MCP 表
theme: dark
font_size: 14
"""

# DSH 的 cordis.patch.yml 是**顶层数组**（- id: 改某条 / - insert: 往列表里插），没有 plugins: 键
CORDIS_CFG = """# cordis 补丁（DSH）
- insert:
    - id: other-plugin
      name: '@deepseek-ai/some-plugin'
- id: system-prompt
  config:
    safety: off
"""

# ⚠️ 这一份就是**现网被写坏的样子**（2026-10-05 晚在 ~/.dsh/profiles/web/cordis.patch.yml 查到的）：
#    command / args 被写成了 config 的同级 → 整份 YAML 解析不过。
CORDIS_BROKEN = """# cordis 补丁（DSH，这条 command/args 被写坏了 —— 整份解析不过）
- insert:
    - id: mcp-cordisfix
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: brain
        transport: stdio
    command: 'C:\\Python311\\python.exe'
    args: ['old.py', '--agent', 'cordisfix', '--mcp']
        cwd: 'D:/core'
"""

CASES = [
    dict(aid="claudejson", name="ClaudeDesktopLike", tier="write", app="ClaudeLike",
         rel="%APPDATA%/ClaudeLike/claude_desktop_config.json", kind="mcp-json",
         seed='{\n  "mcpServers": {\n    "other-one": {"command": "node", "args": ["x.js"]}\n  },\n'
              '  "globalShortcut": "ctrl+space"\n}\n',
         fmt="json", shape="mcpServers", foreign=["other-one"], want_add="写入"),
    dict(aid="codexlike", name="CodexLike", tier="write", app="CodexLike",
         rel="~/.codex/config.toml", kind="codex-toml", seed=CODE_CFG,
         fmt="toml", shape="mcp_servers", foreign=["other"],
         comment="# 我的 codex 配置（这行注释不许被吃掉）", want_add="写入"),
    dict(aid="zedlike", name="ZedLike", tier="read", app="ZedLike",
         rel="%APPDATA%/ZedLike/settings.json", face="json/context_servers",
         seed='{\n  "context_servers": {\n    "other-one": {"command": "node", "args": []}\n  },\n'
              '  "theme": "One Dark"\n}\n',
         fmt="json", shape="context_servers", foreign=["other-one"], want_add="写入"),
    dict(aid="continuelike", name="ContinueLike", tier="write", app="ContinueLike",
         rel="~/.continue/config.yaml", face="yaml/mcpServers", seed=CONTINUE_CFG,
         fmt="yaml", shape="mcpServers", foreign=["other-one"],
         comment="# continue 的配置", want_add="写入"),
    dict(aid="themeapp", name="ThemeOnlyApp", tier="write", app="ThemeApp",
         rel="%APPDATA%/ThemeApp/settings.yaml", face="yaml/mcpServers", seed=THEME_CFG,
         fmt="yaml", shape="mcpServers", foreign=[], comment="# 只有主题，没有任何 MCP 表",
         want_add="写入"),
    dict(aid="autodetect", name="MysteryApp", tier="write", app="MysteryApp",
         rel="%APPDATA%/MysteryApp/mcp.json",          # 不写 kind、不写 format/shape → 看文件自己认
         seed='{"mcpServers": {"other-one": {"command": "node"}}}\n',
         fmt="json", shape="mcpServers", foreign=["other-one"], want_add="写入"),
    dict(aid="reasonixlike", name="ReasonixLike", tier="write", app="ReasonixLike",
         rel="%APPDATA%/ReasonixLike/config.toml", kind="reasonix-toml", seed=REASONIX_CFG,
         fmt="toml", shape="plugins_list", foreign=["mimo-vision", "obsidian"],
         comment="# reasonix 的 External MCP servers", want_add="官方 CLI",
         want_ok=False, want_same=True),
    dict(aid="cordislike", name="CordisLike", tier="trust", app="CordisLike",
         rel="%APPDATA%/CordisLike/cordis.patch.yml", kind="dsh-plugin", seed=CORDIS_CFG,
         fmt="yaml", shape="plugins_list", foreign=["other-plugin"],
         comment="# cordis 补丁（DSH）", want_add=None),
    dict(aid="cordisfix", name="CordisFix", tier="write", app="CordisFix",
         rel="%APPDATA%/CordisFix/cordis.patch.yml", kind="dsh-plugin", seed=CORDIS_BROKEN,
         fmt="yaml", shape="plugins_list", foreign=[], want_add="已按模板更新"),
    dict(aid="brokenapp", name="BrokenConfigApp", tier="write", app="BrokenApp",
         rel="%APPDATA%/BrokenApp/mcp.json", kind="mcp-json", seed='{ "mcpServers": {\n',
         fmt="json", shape="mcpServers", foreign=[], want_add="解析不过",
         want_ok=False, want_same=True),
    dict(aid="noconfig", name="NoConfigApp", tier="write", app="NoConfigApp",
         rel="%APPDATA%/NoConfigApp/mcp.json", kind="mcp-json", seed=None,
         fmt=None, shape=None, foreign=[], want_add="没找到配置文件",
         want_ok=False, want_file=False),
]


def desc_text(c):
    head = ["id: %s" % c["aid"], "name: %s" % c["name"],
            "agent_id: agent.%s" % c["aid"], "plugin_id: plugin.%s" % c["aid"],
            "detect:", '  paths: ["%%APPDATA%%/%s"]' % c["app"],
            '  files: ["%s"]' % c["rel"],
            "interface:", "  tier: %s" % c["tier"],
            "  status_tool: %s.status" % c["aid"],
            "mcp:", "  server_name: brain", "  transport: stdio",
            '  command: "%s"' % posix(pyw()),
            '  args: ["D:/core/plugins/agent_plugin.py", "--agent", "%s", "--mcp"]'
            % c["aid"],
            '  cwd: "D:/core"']
    if c.get("kind"):
        head.append("  kind: %s" % c["kind"])
    if c.get("face"):
        f, s = c["face"].split("/")
        head += ["  format: %s" % f, "  shape: %s" % s]
    head += ['  config: "%s"' % c["rel"], "hook:", "  kind: none", "source:", "  events: none"]
    return "\n".join(head) + "\n"


def abs_cfg(c):
    p = c["rel"].replace("%APPDATA%", FAKE_APPDATA).replace("%LOCALAPPDATA%", FAKE_LOCAL)
    if p.startswith("~"):
        p = FAKE_HOME + p[1:]
    return os.path.normpath(p)


def plant():
    for c in CASES:
        p = abs_cfg(c)
        if c.get("seed") is None:
            continue
        d = os.path.dirname(p)
        if not os.path.isdir(d):
            os.makedirs(d)
        io.open(p, "w", encoding="utf-8", newline="\n").write(c["seed"])
        if c["aid"] == "noconfig":
            continue
        # 每个宿主目录留一个"它装在这儿"的痕迹（detect.paths 认这个目录）
        appdir = os.path.join(FAKE_APPDATA, c["app"])
        if not os.path.isdir(appdir):
            os.makedirs(appdir)
        for other in CASES:
            if other["aid"] == "noconfig":
                continue
            od = os.path.join(FAKE_APPDATA, other["app"])
            if not os.path.isdir(od):
                os.makedirs(od)


def env_child():
    e = dict(os.environ)
    e.update({"USERPROFILE": FAKE_HOME, "HOME": FAKE_HOME,
              "APPDATA": FAKE_APPDATA, "LOCALAPPDATA": FAKE_LOCAL,
              "AGENTCTL_AGENTS_DIR": FAKE_AGENTS, "AGENTCTL_CONFIG": FAKE_CONFIG,
              "AGENTCTL_PERM_FILE": FAKE_PERM, "AGENTCTL_SIM": "1",
              "AGENTCTL_NO_RELOAD": "1",     # 模拟机绝不碰真内核的插件表
              "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    return e


def run(args, env, timeout=600):
    r = subprocess.run([sys.executable, os.path.join(HERE, "agentctl.py")] + args,
                       cwd=CORE, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def mcp_line(sec):
    """一段 add 输出里的『③ MCP 配置』那行（比对幂等话术只看它）"""
    return next((l.strip() for l in sec.splitlines() if "MCP 配置" in l), "")


def sections(text, sep="──"):
    """把 cmd_add / cmd_verify 的输出按 agent 切开：{aid: 该段文本}"""
    out, cur = {}, None
    for ln in text.splitlines():
        if ln.startswith(sep + " "):
            head = ln[len(sep) + 1:].strip()
            cur = head.split("（")[0].replace("验收", "").strip()
            out[cur] = []
        elif cur:
            out[cur].append(ln)
    return {k: "\n".join(v) for k, v in out.items()}


def main():
    print("外机模拟器（假电脑：%s）" % SIMROOT)
    print("-" * 74)
    real_before = (sha(REAL_CONFIG), sha(REAL_PERM))
    if os.path.isdir(SIMROOT):
        shutil.rmtree(SIMROOT, ignore_errors=True)
    for d in (FAKE_HOME, FAKE_APPDATA, FAKE_LOCAL, FAKE_AGENTS,
              os.path.dirname(FAKE_PERM)):
        os.makedirs(d)
    shutil.copy2(REAL_CONFIG, FAKE_CONFIG)
    shutil.copy2(REAL_PERM, FAKE_PERM)
    cfg_before = io.open(FAKE_CONFIG, encoding="utf-8").read()
    perm_before = json.loads(io.open(FAKE_PERM, encoding="utf-8").read() or "{}")

    plant()
    ids = [c["aid"] for c in CASES]
    for c in CASES:
        io.open(os.path.join(FAKE_AGENTS, c["aid"] + ".yaml"), "w",
                encoding="utf-8", newline="\n").write(desc_text(c))
    env = env_child()
    print("① 假机器：%d 个 agent（%d 份配置已就位）" % (len(CASES), len(ids)))

    # ── plan（只看，不落盘）────────────────────────────────────────
    rc, out = run(["plan"] + ids, env)
    check("plan 跑得通（exit=0）", rc == 0, out[-400:])
    for c in CASES:
        if c.get("seed") is None:
            continue
        check("plan 认到 %s 的配置面" % c["aid"], c["rel"].split("/")[-1] in out, c["rel"])

    # ── add --apply（第一次：真写）─────────────────────────────────
    rc, out = run(["add"] + ids + ["--apply"], env)
    check("add --apply 跑得通（exit=0）", rc == 0, out[-400:])
    sec = sections(out)
    for c in CASES:
        if c.get("want_add"):
            check("%s：add 说的话对（含「%s」）" % (c["aid"], c["want_add"]),
                  c["want_add"] in sec.get(c["aid"], ""), sec.get(c["aid"], "")[:200])

    print("② 写完逐个读回（形状 / 我们的条目 / 别人的条目 / 注释）")
    for c in CASES:
        p = abs_cfg(c)
        if c.get("want_file") is False:
            check("%s：全程没建文件（该没装就没装）" % c["aid"], not os.path.exists(p), p)
            continue
        if not c.get("fmt") or c.get("kind") == "dsh-plugin":
            continue                                     # cordis 那种特殊形状单独查（下面）
        if c.get("kind") == "reasonix-toml":
            check("%s：只读形状一个字节没动" % c["aid"],
                  io.open(p, encoding="utf-8").read() == c["seed"], p)
            continue
        if c.get("want_ok") is False:
            check("%s：坏文件一个字节没动" % c["aid"],
                  io.open(p, encoding="utf-8").read() == c["seed"], p)
            continue
        raw = io.open(p, encoding="utf-8").read()
        check("%s：写完对应解析器读得动" % c["aid"], MH.parse_ok(p, c["fmt"]), raw[:120])
        found, marker, blob = MH.read_entry(p, c["fmt"], c["shape"], "brain")
        check("%s：我们那条在（%s × %s）" % (c["aid"], c["fmt"], c["shape"]), found and marker, blob)
        check("%s：那条里带平台特征" % c["aid"], "agent_plugin.py" in blob, blob)
        check("%s：命令是 pythonw（不弹黑窗）" % c["aid"], "python.exe" not in blob, blob)
        names = MH.entry_names(p, c["fmt"], c["shape"])
        check("%s：别人的条目还在（%s）" % (c["aid"], ",".join(c["foreign"]) or "无"),
              all(x in names for x in c["foreign"]), names)
        if c.get("comment"):
            check("%s：注释没被吃掉" % c["aid"], c["comment"] in raw, raw[:200])

    # cordis（DSH）两种：① 往现成的 `- insert:` 列表里插 ② 修好被写坏的现有条目
    import yaml
    print("②·b cordis（DSH）：插进现成 - insert: / 修好坏掉的现有条目")
    for _aid, _new in (("cordislike", True), ("cordisfix", False)):
        _c = next(c for c in CASES if c["aid"] == _aid)
        _p = abs_cfg(_c)
        _raw = io.open(_p, encoding="utf-8").read()
        check("%s：整份 YAML 解析得过" % _aid, MH.parse_ok(_p, "yaml"), _raw[:160])
        _d = yaml.safe_load(_raw) or []
        _ins = next((x for x in _d if isinstance(x, dict) and "insert" in x), {})
        _items = [x for x in (_ins.get("insert") or []) if isinstance(x, dict)]
        _ids = [x.get("id") for x in _items]
        check("%s：我们那条在 - insert: 列表里（%s）" % (_aid, _ids), ("mcp-" + _aid) in _ids, _ids)
        _ent = next((x for x in _items if x.get("id") == "mcp-" + _aid), {})
        _cfg = _ent.get("config") or {}
        check("%s：command/args 在 config 下面（结构对，不是同级）" % _aid,
              "command" in _cfg and "args" in _cfg, _cfg)
        check("%s：args 指向 agent_plugin.py" % _aid,
              any("agent_plugin.py" in str(a) for a in (_cfg.get("args") or [])), _cfg.get("args"))
        check("%s：命令是 pythonw（不弹黑窗）" % _aid, "python.exe'" not in _raw, _raw[-200:])
        check("%s：注释没被吃掉" % _aid, "# cordis 补丁（DSH" in _raw, _raw[:120])
        if _new:
            check("%s：别人的条目没被冲掉" % _aid, "other-plugin" in _ids, _ids)
            check("%s：顶层别的 patch 条目没被碰（system-prompt）" % _aid,
                  any(isinstance(x, dict) and x.get("id") == "system-prompt" for x in _d), _d)
        else:
            check("%s：原来那条被修好（old.py 没了）" % _aid, "old.py" not in _raw, _raw[-300:])

    # ── 第一次 add 之后：平台侧两处登记 ────────────────────────────
    print("③ 平台侧两处登记（config.yaml 条目 + 档位）")
    cfg = io.open(FAKE_CONFIG, encoding="utf-8").read()
    perm = json.loads(io.open(FAKE_PERM, encoding="utf-8").read() or "{}")
    for c in CASES:
        check("%s：config.yaml 里有且只有一条接口条目" % c["aid"],
              cfg.count("- id: plugin.%s" % c["aid"]) == 1,
              cfg.count("- id: plugin.%s" % c["aid"]))
        check("%s：档位显式登记成 %s" % (c["aid"], c["tier"]),
              perm.get("plugin.%s" % c["aid"]) == c["tier"], perm.get("plugin.%s" % c["aid"]))
    for k, v in perm_before.items():
        check("原有的档位没被碰（%s=%s）" % (k, v), perm.get(k) == v, perm.get(k))

    # ── add 第二次：幂等（不重复插、不再改文件）───────────────────
    print("④ 再跑一遍 add --apply（幂等）")
    before = {c["aid"]: (io.open(abs_cfg(c), encoding="utf-8").read()
                         if os.path.exists(abs_cfg(c)) else "") for c in CASES}
    rc, out2 = run(["add"] + ids + ["--apply"], env)
    check("第二次 add 也跑得通（exit=0）", rc == 0, out2[-300:])
    cfg2 = io.open(FAKE_CONFIG, encoding="utf-8").read()
    for c in CASES:
        check("%s：第二次 add 没再写文件" % c["aid"],
              (io.open(abs_cfg(c), encoding="utf-8").read()
               if os.path.exists(abs_cfg(c)) else "") == before[c["aid"]], abs_cfg(c))
        check("%s：config.yaml 还是只有一条" % c["aid"],
              cfg2.count("- id: plugin.%s" % c["aid"]) == 1, cfg2.count("- id: plugin.%s" % c["aid"]))
    sec2 = sections(out2)
    _idem = [c["aid"] for c in CASES if c.get("want_ok") is not False]
    check("第二次 add 对『该写进去的』说「已在」（幂等话术）",
          all("已在" in sec2.get(a, "") for a in _idem),
          {a: sec2.get(a, "")[:80] for a in _idem if "已在" not in sec2.get(a, "")})
    _nofirst = [c["aid"] for c in CASES if c.get("want_ok") is False]
    check("第一轮没写的那几类（只读/坏文件/没装），第二轮 MCP 那行的话一模一样（不许忽然开始写）",
          all(mcp_line(sec2.get(a, "")) == mcp_line(sec.get(a, "")) for a in _nofirst),
          {a: (mcp_line(sec.get(a, "")), mcp_line(sec2.get(a, ""))) for a in _nofirst
           if mcp_line(sec2.get(a, "")) != mcp_line(sec.get(a, ""))})

    # ── verify（按面逐个验收）────────────────────────────────────
    print("⑤ verify：每个面各一行（内核那几条 ❌ 是预期的：假 agent 没接口进程）")
    rc, vout = run(["verify"] + ids, env)
    vsec = sections(vout)
    for c in CASES:
        body = vsec.get(c["aid"], "")
        if (c.get("want_ok") is False and c.get("seed") is not None
                and c.get("kind") != "dsh-plugin"):
            check("%s：verify 说这个面没配（诚实）" % c["aid"], "（没配）" in body, body[:200])
        elif c.get("seed") is None:
            check("%s：verify 说这个面没装（跳过）" % c["aid"], "没装/跳过" in body, body[:200])
        else:
            check("%s：verify 说这个面在位（✅）" % c["aid"], "（在位）" in body, body[:200])
    check("verify 里接口没登记（预期：内核不认识假 agent）", "接口已登记" in vout, vout[:200])

    # ── 真机没被碰 ────────────────────────────────────────────────
    print("⑥ 真机（本仓库）没被这次演练碰过")
    check("真 config.yaml 没变", sha(REAL_CONFIG) == real_before[0])
    check("真 permissions.json 没变", sha(REAL_PERM) == real_before[1])
    check("假机 config.yaml 是从真的拷来的（演练有效）", cfg_before == io.open(
        REAL_CONFIG, encoding="utf-8").read())

    print("-" * 74)
    if FAILS:
        print("失败 %d 条（共 %d 项）：%s" % (len(FAILS), NCHK, "、".join(FAILS[:8])))
        return 1
    print("全部通过（%d 项，10 种形状：JSON/TOML/YAML × mcpServers/mcp_servers/context_servers/"
          "plugins_list + 坏文件 + 无配置）" % NCHK)
    print("假机器留在：%s（自己看一眼可以删）" % SIMROOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
