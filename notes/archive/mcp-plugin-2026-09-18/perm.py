#!/usr/bin/env python
"""agent 插件的权限管理（命令行）。

权限分三处放（都在 agent/ 里，不碰库本体）：
  · 档   —— 每个工具天生属于 read / write / danger（写在 mcp_tools.py 里）
  · 暴露 —— agent/config.json 的 expose_tiers：只有这些档的工具才会出现在 agent 的工具清单里
  · 确认 —— agent/config.json 的 require_confirm：这些工具必须带 confirm=true 才执行
           （danger 档**不管怎么配都要确认**，这是硬规则）
  · 拉黑 —— agent/config.json 的 disabled_tools：直接不给

用法：
  python agent/perm.py                      # 看当前权限表
  python agent/perm.py --json               # 机器可读
  python agent/perm.py expose read,write    # 设暴露哪些档（支持 +danger / -write 增删）
  python agent/perm.py require reindex,set_note_rag      # 这些必须确认
  python agent/perm.py unrequire reindex                 # 取消"必须确认"
  python agent/perm.py hide search_notes                 # 拉黑
  python agent/perm.py show search_notes                 # 解除拉黑
  python agent/perm.py reset                             # 回到默认（read+write 暴露）
  python agent/perm.py set base_url http://127.0.0.1:8848
加 --config <路径> 可以操作另一份配置（测试用）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import mcp_tools  # noqa: E402

DEFAULT = {"base_url": "http://127.0.0.1:8848", "timeout_sec": 120,
           "expose_tiers": ["read", "write"], "require_confirm": ["set_note_rag", "reindex"],
           "disabled_tools": []}
TIERS = ("read", "write", "danger")
NOTE = ("权限在这里调：expose_tiers 决定暴露哪些档（read/write/danger）；require_confirm 里的工具"
        "必须带 confirm=true 才会执行；disabled_tools 直接拉黑。")


def load(path: str) -> dict:
    cfg = dict(DEFAULT)
    cfg["_说明"] = NOTE
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print("⚠️ 配置读不动（%s），先用默认值" % str(e)[:60])
    cfg.setdefault("_说明", NOTE)
    return cfg


def save(path: str, cfg: dict) -> None:
    body = {k: v for k, v in cfg.items() if not k.startswith("_")}
    body = {"base_url": body.get("base_url"), "timeout_sec": body.get("timeout_sec"),
            "_说明": NOTE,
            "expose_tiers": body.get("expose_tiers") or [],
            "require_confirm": body.get("require_confirm") or [],
            "disabled_tools": body.get("disabled_tools") or []}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(body, f, ensure_ascii=False, indent=2)
        f.write("\n")


def effective(cfg: dict) -> list:
    """每个工具当前生效的权限：{name, tier, exposed, confirm, why}"""
    tiers = set(cfg.get("expose_tiers") or [])
    off = set(cfg.get("disabled_tools") or [])
    req = set(cfg.get("require_confirm") or [])
    out = []
    for t in mcp_tools.TOOLS:
        exposed = t["tier"] in tiers and t["name"] not in off
        confirm = t["tier"] == "danger" or t["name"] in req
        why = []
        if t["name"] in off:
            why.append("已拉黑")
        elif t["tier"] not in tiers:
            why.append("档没开(%s)" % t["tier"])
        if t["tier"] == "danger":
            why.append("危险档硬性要确认")
        elif t["name"] in req:
            why.append("配置要求确认")
        out.append({"name": t["name"], "tier": t["tier"], "desc": t["desc"],
                    "exposed": exposed, "confirm": confirm, "why": "；".join(why)})
    return out


def render(rows: list) -> str:
    lines = []
    for tier in TIERS:
        sub = [r for r in rows if r["tier"] == tier]
        if not sub:
            continue
        label = {"read": "只读", "write": "写（可撤销）", "danger": "危险"}[tier]
        lines.append("── %s（%d 个） ──" % (label, len(sub)))
        for r in sub:
            state = "✅暴露" if r["exposed"] else "⛔不给"
            conf = "🔒要确认" if r["confirm"] else "  免确认"
            lines.append("   %-6s %-22s %s  %s  %s" % (state, r["name"], conf,
                                                       ("档 " + r["tier"]).ljust(12), r["desc"][:38]))
            if r["why"] and not r["exposed"]:
                lines.append("        ↳ 原因：%s" % r["why"])
    return "\n".join(lines)


def names_arg(s: str) -> list:
    return [x.strip() for x in (s or "").replace("，", ",").split(",") if x.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("expose")
    p.add_argument("tiers")
    p = sub.add_parser("require")
    p.add_argument("tools")
    p = sub.add_parser("unrequire")
    p.add_argument("tools")
    p = sub.add_parser("hide")
    p.add_argument("tool")
    p = sub.add_parser("show")
    p.add_argument("tool")
    sub.add_parser("reset")
    p = sub.add_parser("set")
    p.add_argument("key")
    p.add_argument("value")
    a = ap.parse_args()

    path = a.config
    cfg = load(path)
    changed = False

    if a.cmd == "expose":
        cur = set(cfg.get("expose_tiers") or [])
        for tok in names_arg(a.tiers):
            if tok.startswith("+"):
                cur.add(tok[1:])
            elif tok.startswith("-"):
                cur.discard(tok[1:])
            else:
                cur = set(x for x in names_arg(a.tiers) if x in TIERS)
                break
        bad = [x for x in cur if x not in TIERS]
        if bad:
            print("❌ 不认识的档：%s（只有 read/write/danger）" % "、".join(bad))
            return 2
        cfg["expose_tiers"] = [t for t in TIERS if t in cur]
        changed = True
    elif a.cmd in ("require", "unrequire"):
        cur = set(cfg.get("require_confirm") or [])
        want = names_arg(a.tools)
        bad = [x for x in want if x not in mcp_tools.BY_NAME]
        if bad:
            print("❌ 没有这些工具：%s" % "、".join(bad))
            return 2
        cur = (cur | set(want)) if a.cmd == "require" else (cur - set(want))
        cfg["require_confirm"] = sorted(cur)
        changed = True
    elif a.cmd in ("hide", "show"):
        cur = set(cfg.get("disabled_tools") or [])
        if a.tool not in mcp_tools.BY_NAME:
            print("❌ 没有这个工具：%s" % a.tool)
            return 2
        cur = (cur | {a.tool}) if a.cmd == "hide" else (cur - {a.tool})
        cfg["disabled_tools"] = sorted(cur)
        changed = True
    elif a.cmd == "reset":
        cfg = dict(DEFAULT)
        cfg["_说明"] = NOTE
        changed = True
    elif a.cmd == "set":
        cfg[a.key] = a.value
        changed = True

    if changed:
        save(path, cfg)
        print("✍️ 已改 %s" % path)

    rows = effective(cfg)
    if a.json:
        print(json.dumps({"config": path, "base_url": cfg.get("base_url"),
                          "expose_tiers": cfg.get("expose_tiers"),
                          "require_confirm": cfg.get("require_confirm"),
                          "disabled_tools": cfg.get("disabled_tools"),
                          "tools": rows}, ensure_ascii=False, indent=1))
        return 0
    print("配置文件：%s" % path)
    print("暴露的档：%s ｜ 必须确认：%s ｜ 已拉黑：%s"
          % (",".join(cfg.get("expose_tiers") or []) or "（无）",
             ",".join(cfg.get("require_confirm") or []) or "（无）",
             ",".join(cfg.get("disabled_tools") or []) or "（无）"))
    print(render(rows))
    n_exp = sum(1 for r in rows if r["exposed"])
    print("\n给 agent 的工具：%d 个（共 %d 个）" % (n_exp, len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
