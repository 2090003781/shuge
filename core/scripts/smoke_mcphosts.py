#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""多格式 MCP 写入自测 —— **在假机器上模拟多种 agent 形状**（不动真机上的任何配置）。

辞玖 2026-10-05 晚：「配置文件考虑一下 toml、config、yaml 等多格式问题…在别人电脑上不会有信给你，模拟多试几种情况」。
所以这里造一棵临时树，把已知的几种宿主形状都摆出来，逐一验四件事：
  ① 认得出格式与形状  ② 写得进去（别人的条目、注释都不许动）  ③ 写完能回读校验  ④ 幂等 + 坏文件不写

跑：python scripts\smoke_mcphosts.py
"""
import io
import os
import shutil
import sys

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(CORE, "scripts"))
import mcphosts as M  # noqa: E402

ROOT = os.path.join(CORE, "data", "tmp", "mcphost-sim")
CMD = "C:/Python311/pythonw.exe"
ARGS = ["D:/core/plugins/agent_plugin.py", "--agent", "sim", "--mcp"]
CWD = "D:/core"
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  <- " + str(extra)))
    if not cond:
        fails.append(name)


def mk(name, text):
    p = os.path.join(ROOT, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    io.open(p, "w", encoding="utf-8", newline="\n").write(text)
    return p


# ── 各种形状的样例（都带"别人的条目"，有的还带注释）──
CASES = [
    ("json", "mcpServers", "a/mcp.json",
     '{\n  "mcpServers": {\n    "other-one": {"command": "x", "args": ["y"]}\n  }\n}\n', None, None),
    ("toml", "mcp_servers", "b/config.toml",
     '# Codex 风格：mcp_servers 表\nmodel = "gpt"\n\n[mcp_servers.other-one]\ncommand = "x"\nargs = ["y"]\n', "#", None),
    ("toml", "plugins_list", "c/config.toml",
     '# External MCP servers（Reasonix 风格：[[plugins]] 数组）\n[[plugins]]\nname = "mimo-vision"\ncommand = "python x.py"\n\n[[plugins]]\nname = "obsidian"\ncommand = "npm x"\n', "#", None),
    ("yaml", "mcpServers", "d/cordis.patch.yml",
     '# DSH 风格\nmcpServers:\n  other-one:\n    command: x\n    args: ["y"]\n', "#", None),
    ("yaml", "context_servers", "e/settings.json.yaml",
     '# Zed 风格：context_servers\ncontext_servers:\n  other-one:\n    command: x\n', "#", None),
    ("yaml", "mcpServers", "f/only_theme.yaml",
     '# 只有别的东西，没有 MCP 表 —— 我们该自己建一块\ntheme: dark\n', "#", None),
    ("json", "mcpServers", "g/broken.json", '{ "mcpServers": {  \n', "#", "refuse"),
    ("toml", "mcp_servers", "h/broken.toml", '[mcp_servers\ncommand = "x"\n', "#", "refuse"),
    ("yaml", "mcpServers", "i/inline.yaml", 'mcpServers: {}\n', "#", "refuse"),
]

if os.path.isdir(ROOT):
    shutil.rmtree(ROOT, ignore_errors=True)
print("多格式 MCP 写入自测（假机器：%s）" % os.path.relpath(ROOT, CORE))
print("-" * 66)
for i, (fmt, shape, rel, text, comment, mode) in enumerate(CASES, 1):
    p = mk(rel, text)
    tag = "%s/%s" % (fmt, shape)
    before = io.open(p, encoding="utf-8").read()
    other = M.entry_names(p, fmt, shape)
    got_fmt, hits = M.detect_shapes(p)
    if mode == "refuse":
        ok, did, _ = M.write_entry(p, fmt, shape, "brain", CMD, ARGS, CWD)
        after = io.open(p, encoding="utf-8").read()
        check("%d. %s（%s）：坏文件**拒绝写**且一个字节没动" % (i, rel, tag),
              (not ok) and after == before, did)
        continue
    check("%d. %s：格式认得对（%s）" % (i, rel, got_fmt), got_fmt == fmt, got_fmt)
    if "only_theme" in rel:
        check("%d. %s：本来就没有 MCP 表（认不出形状是对的）" % (i, rel), not hits, hits)
    else:
        check("%d. %s：形状认得出来（%s）" % (i, rel, hits), any(h[0] == shape for h in hits), hits)
    ok, did, _ = M.write_entry(p, fmt, shape, "brain", CMD, ARGS, CWD)
    check("%d. %s：写得进去（%s）" % (i, rel, did), ok, did)
    check("%d. %s：写完还能解析" % (i, rel), M.parse_ok(p, fmt), "")
    found, marked, blob = M.read_entry(p, fmt, shape, "brain")
    check("%d. %s：回读得到我们那条（带平台特征）" % (i, rel), found and marked, blob[:80])
    names = M.entry_names(p, fmt, shape)
    check("%d. %s：别人的条目还在（%s）" % (i, rel, other), all(x in names for x in other), names)
    if comment:
        check("%d. %s：注释没被吃掉" % (i, rel), comment in io.open(p, encoding="utf-8").read(), "")
    snap = io.open(p, encoding="utf-8").read()
    ok2, did2, _ = M.write_entry(p, fmt, shape, "brain", CMD, ARGS, CWD)
    check("%d. %s：幂等（再写一次说已在、文件不变）" % (i, rel),
          ok2 and "已在" in did2 and io.open(p, encoding="utf-8").read() == snap, did2)

# ── 面判读：写 / 验收 / 列表**共用** face_of 这一份判据（不是只看落盘）────────
print("-" * 66)
print("面判读（face_of）")
FACE_CASES = [
    (dict(kind="mcp-json"), ("json", "mcpServers", True, "kind")),
    (dict(kind="codex-toml"), ("toml", "mcp_servers", True, "kind")),
    (dict(kind="reasonix-toml"), ("toml", "plugins_list", False, "kind")),
    (dict(kind="none"), ("none", "none", False, "unknown")),
    (dict(kind="", fmt="yaml", shape="context_servers"), ("yaml", "context_servers", True, "explicit")),
    # mcp_hosts 会把"没写 kind"规范成 none —— 这时**显式的格式/形状不能被判成只读**
    # （2026-10-05 模拟器第三轮逮到：zedlike / continuelike / themeapp 全被这句挡掉）
    (dict(kind="none", fmt="json", shape="mcpServers"), ("json", "mcpServers", True, "explicit")),
    (dict(kind="none", fmt="yaml"), ("yaml", "none", False, "explicit")),
]
for _kw, _want in FACE_CASES:
    _got = M.face_of(**_kw)
    check("face_of(%s) = %s" % (_kw, _want), _got == _want, _got)

print("-" * 66)
print("模拟树留在：%s（自己看一眼可以删）" % ROOT)
if fails:
    print("失败 %d 条：%s" % (len(fails), "、".join(fails)))
    sys.exit(1)
print("全部通过（%d 种情形）" % len(CASES))
