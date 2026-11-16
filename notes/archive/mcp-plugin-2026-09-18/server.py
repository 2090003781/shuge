#!/usr/bin/env python
"""ai-notes 的 MCP 插件（stdio，只用标准库）。

把库的能力以 MCP 工具暴露给 agent。**权限在这一层调**（`agent/config.json`）：
  expose_tiers    暴露哪些档：read / write / danger（默认只暴露 read+write）
  require_confirm 这些工具必须带 confirm=true 才执行（危险的默认全要确认）
  disabled_tools  直接拉黑某些工具

用法（在 MCP 客户端里注册）：
  command: python
  args:    ["D:\\core-notes\\agent\\server.py"]
环境变量 AI_NOTES_MCP_CONFIG 可以指向另一个配置文件（换一套权限）。

想看 / 想改权限（不用起服务）：
  python agent/server.py --print-perms     # 当前权限表（每个工具：暴露没 / 要不要确认）
  python agent/server.py --print-tools     # 当前会给 agent 的工具清单（JSON）
  python agent/perm.py                     # 同一个表，还能改（expose / require / hide / reset）
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import mcp_tools  # noqa: E402

# 支持的 MCP 协议版本（对齐 @modelcontextprotocol/sdk 1.30 的 SUPPORTED_PROTOCOL_VERSIONS）：
# 客户端要哪个、我们支持就回哪个；要了个我们不认识的，就回我们最新的那个。
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07")
PROTOCOL = SUPPORTED_PROTOCOLS[0]


def negotiate(params) -> str:
    want = str((params or {}).get("protocolVersion") or "")
    return want if want in SUPPORTED_PROTOCOLS else PROTOCOL
SERVER_INFO = {"name": "ai-notes", "version": "1.0.0"}
CONFIG_PATH = os.environ.get("AI_NOTES_MCP_CONFIG") or os.path.join(HERE, "config.json")


def load_config() -> dict:
    cfg = {"base_url": "http://127.0.0.1:8848", "timeout_sec": 120,
           "expose_tiers": ["read", "write"], "require_confirm": [], "disabled_tools": []}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


CFG = load_config()
_CFG_MTIME = None


def cfg() -> dict:
    """取配置；**每次用之前看一眼文件有没有被改** —— 权限改完不用重启 MCP 客户端就生效"""
    global CFG, _CFG_MTIME
    try:
        m = os.path.getmtime(CONFIG_PATH)
    except OSError:
        m = None
    if m != _CFG_MTIME:
        CFG = load_config()
        _CFG_MTIME = m
    return CFG


def exposed() -> list:
    c = cfg()
    tiers = set(c.get("expose_tiers") or [])
    off = set(c.get("disabled_tools") or [])
    return [t for t in mcp_tools.TOOLS if t["tier"] in tiers and t["name"] not in off]


def needs_confirm(tool: dict) -> bool:
    return tool["tier"] == "danger" or tool["name"] in (cfg().get("require_confirm") or [])


def http(method: str, path: str, query: dict | None = None, body: dict | None = None) -> dict:
    c = cfg()
    url = c["base_url"].rstrip("/") + path
    if query:
        clean = {k: v for k, v in query.items() if v not in (None, "", [])}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=float(c.get("timeout_sec") or 120)) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": "HTTP %s" % e.code}
    except Exception as e:
        return {"ok": False, "error": "连不上库（%s）：%s" % (c["base_url"], str(e)[:120])}


def call_tool(name: str, args: dict) -> dict:
    tool = mcp_tools.BY_NAME.get(name)
    if tool is None or tool not in exposed():
        return {"isError": True, "text": "没有这个工具（或没在 agent/config.json 里开放）：%s" % name}
    if needs_confirm(tool) and not args.get("confirm"):
        return {"isError": True,
                "text": "「%s」属于需要确认的操作。确认要执行就再调一次，参数里带 "
                        "confirm=true；否则请先跟人确认。" % name}
    c = tool["call"]
    path, query, body = c["path"], {}, None
    for k in (c.get("query") or []):
        if k in args:
            query[k] = args[k]
    for k in (c.get("body") or []):
        if k in args:
            body = body or {}
            body[k] = args[k]
    for k, v in (args or {}).items():                 # 路径参数（{id}）
        if "{%s}" % k in path:
            path = path.replace("{%s}" % k, urllib.parse.quote(str(v)))
    if "{id}" in path and "id" in args:
        path = path.replace("{id}", urllib.parse.quote(str(args["id"])))
    if "{id}" in path:
        return {"isError": True, "text": "这个工具需要 id（文件 id）"}
    r = http(c["method"], path, query, body)
    ok = bool(r.get("ok", True))
    out = json.dumps(r, ensure_ascii=False)
    if len(out) > 120000:                             # 别把上下文塞爆
        out = out[:120000] + "…（截断，用更小的 max/limit 再取）"
    return {"isError": not ok, "text": out}


def handle(req: dict):
    mid = req.get("id")
    method = req.get("method") or ""
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": negotiate(req.get("params")),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO}}
    if method in ("notifications/initialized", "initialized"):
        return None                                    # 通知：不回
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        tools = []
        for t in exposed():
            confirm = needs_confirm(t)
            tools.append({"name": t["name"],
                          "description": t["desc"] + ("（需 confirm=true）" if confirm else ""),
                          "inputSchema": t["schema"]})
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}}
    if method == "tools/call":
        p = req.get("params") or {}
        res = call_tool(p.get("name") or "", p.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": res["text"]}],
            "isError": bool(res.get("isError"))}}
    if mid is None:
        return None                                    # 不认识的通知：按协议也不该回话
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": "不认识的方法：%s" % method}}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        try:
            resp = handle(req)
        except Exception as e:
            resp = {"jsonrpc": "2.0", "id": req.get("id"),
                    "error": {"code": -32603, "message": "%s: %s" % (type(e).__name__, e)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


def _cli() -> int:
    """不带参数 = 起 MCP 服务；带参数 = 看权限 / 看工具清单"""
    args = sys.argv[1:]
    if not args:
        return main()
    if "--print-perms" in args:
        import perm
        c = cfg()
        print("配置文件：%s" % CONFIG_PATH)
        print("暴露的档：%s ｜ 必须确认：%s ｜ 已拉黑：%s"
              % (",".join(c.get("expose_tiers") or []) or "（无）",
                 ",".join(c.get("require_confirm") or []) or "（无）",
                 ",".join(c.get("disabled_tools") or []) or "（无）"))
        print(perm.render(perm.effective(c)))
        return 0
    if "--print-tools" in args:
        print(json.dumps([{"name": t["name"], "tier": t["tier"],
                           "confirm": needs_confirm(t), "description": t["desc"]}
                          for t in exposed()], ensure_ascii=False, indent=1))
        return 0
    print("用法：python agent/server.py [--print-perms|--print-tools]（不带参数则起 MCP 服务）")
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
