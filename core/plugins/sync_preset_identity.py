#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync_preset_identity.py — 从 Vault 人设文件把人设正文同步进 xixi 预设的身份段。

为什么需要它：
  DSH 的身份段有两个来源，且 **agent 预设那层会遮蔽 profile 全局层**：
    · profile cordis.patch.yml 的 system-prompt.personaPrefix（全局层）
    · agent 预设的 persona 行（agent 作用域，优先级更高）
  我们的人设归 Vault（second-brain-system/dsh/DSH-人设.md），但预设文件是 YAML，
  不能运行时读文件 → 每次改完人设，跑一次本脚本把人设正文刷进预设的 persona.prefix。

用法：
  python sync_preset_identity.py            # 同步（改 Vault 人设后跑一次）
  python sync_preset_identity.py --check    # 只比对，不写

改完需要重启 dsh web 才生效（预设组装在会话创建时读取）。
"""
import argparse
import io
import os
import re
import sys

VAULT = os.environ.get("VAULT_PATH", r"D:\个人数据\辞玖")
PERSONA_REL = os.path.join("second-brain-system", "dsh", "DSH-人设.md")
# 预设文件：默认从**当前用户**的 .dsh 推（原来写死了用户名与个人预设名，2026-10-06 去掉）
PRESET = os.environ.get(
    "PRESET_FILE",
    os.path.join(os.path.expanduser("~"), ".dsh", ".agent-presets",
                 os.environ.get("PRESET_NAME", "agent"), "agent.cordis.yml"),
)
# 与 dsh-second-brain 插件同款清洗规则：去 frontmatter / 整行 HTML 注释 / 引用行
def clean_identity(raw):
    no_fm = re.sub(r"^---\r?\n[\s\S]*?\r?\n---\r?\n?", "", raw)
    fence = False
    out = []
    for line in re.split(r"\r?\n", no_fm):
        if re.match(r"^\s*```", line):
            fence = not fence
        if fence or (not re.match(r"^\s*<!--", line) and not re.match(r"^\s*>", line)):
            out.append(line)
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def persona_text():
    p = os.path.join(VAULT, PERSONA_REL)
    with io.open(p, encoding="utf-8") as f:
        return clean_identity(f.read())


def render_prefix(text):
    """把文本渲染成 YAML 字面块（每行缩进 6 空格，块标量 |-）"""
    lines = text.split("\n")
    body = "\n".join(("      " + ln) if ln.strip() else "" for ln in lines)
    return "    prefix: |-\n" + body + "\n"


def current_prefix(yml_text):
    """抽出当前 persona 行到下一个顶层 '-' 之间的 prefix 块（含缩进），返回 (原文, 值)"""
    m = re.search(r"^(\s*)- id: persona\s*$", yml_text, re.M)
    if not m:
        raise SystemExit("预设里找不到 persona 行")
    start = m.start()
    nxt = re.search(r"^- id: ", yml_text[m.end():], re.M)
    end = m.end() + (nxt.start() if nxt else len(yml_text) - m.end())
    block = yml_text[start:end]
    # 注意：块标量里可以有**空行**（段落之间）——空行没有 6 空格缩进，
    # 所以匹配段必须是「6 空格开头的行」或「空行」，否则读到第一个空行就截断
    pm = re.search(r"^    prefix: [|>]-?\r?\n((?:(?:      .*)?\r?\n)*)", block, re.M)
    if not pm:
        raise SystemExit("persona 行里找不到 prefix 块")
    value = "\n".join(
        ln[6:] if ln.startswith("      ") else ""
        for ln in pm.group(1).rstrip("\n").split("\n")
    )
    return pm.group(0), value


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    want = persona_text()
    with io.open(PRESET, encoding="utf-8") as f:
        yml = f.read()
    old_block, old_value = current_prefix(yml)

    same = old_value.strip() == want.strip()
    print(f"人设源：{os.path.join(VAULT, PERSONA_REL)}（{len(want)} 字符）")
    print(f"预设文件：{PRESET}")
    print(f"当前预设里的身份段：{len(old_value)} 字符｜一致：{same}")
    if same:
        print("无需同步 ✅")
        return 0
    if args.check:
        print("不一致 ❌（跑一次不带 --check 的同步）")
        return 1

    new_block = render_prefix(want)
    yml = yml.replace(old_block, new_block, 1)
    bak = PRESET + ".bak-autosync"
    if not os.path.exists(bak):
        with io.open(bak, "w", encoding="utf-8", newline="") as f:
            f.write(yml.replace(new_block, old_block, 1))
        print(f"已留首份备份：{bak}")
    with io.open(PRESET, "w", encoding="utf-8", newline="") as f:
        f.write(yml)
    print(f"已同步 ✅（{len(want)} 字符）—— 重启 dsh web 后新会话生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
