#!/usr/bin/env python3
"""agent 接口插件的 **MCP 面**：agent 侧调平台软件操作的**唯一接口**（stdio）。

它不是"把 31 个操作拆成 31 个工具"——MCP 这一面就是**接口**本身：
    · 对外只有一个工具 `brain_ops`（名字可用 --tool 改），入参 {op, args}；
    · `op` 取平台操作目录里的 id（brain.search / brain.create / …），具体有哪些能调、
      归哪一档、放开没放开，**都由平台说了算**：清单从 /api/ops 现取，调用转发 /api/ops/call；
    · **授权判定不在这层**：这里只搬运。越权由平台回执拒掉（写明需要哪一档），
      所以客户端哪怕缓存了旧清单，也不会真做出越权的事。

谁在用：plugin.dsh / plugin.reasonix —— 各自用 `--mcp` 起一个属于自己 id 的接口。

协议范围：工具桥接所需的最小集（initialize / ping / tools/list / tools/call）。
stdout 只走协议（一行一条 JSON-RPC），日志一律进 stderr。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:8790")
# __file__ = <core>/plugins/lib/ops_mcp.py → 上三级就是平台根
CORE_DIR = os.environ.get("CORE_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LEVEL_CN = {"read": "只读", "write": "可写", "trust": "相信"}
DEFAULT_TOOL = "brain_ops"
SERVER_VERSION = "1.0"
FALLBACK_PROTOCOL = "2025-03-26"
# 客户端版本由它自己报，回同一个即可（官方 SDK 会校验响应版本在它支持列表内）
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07")

LIST_WORDS = ("list", "help", "清单", "有哪些")


def load_token():
    """插件面 token：环境变量优先（内核 spawn 插件时注入），否则读 data/plugins/token.json。

    MCP 面是 agent 宿主拉起的进程，拿不到内核注入的环境变量，所以文件兜底是必需的。
    """
    tok = os.environ.get("PLUGIN_TOKEN", "")
    if tok:
        return tok
    try:
        with open(os.path.join(CORE_DIR, "data", "plugins", "token.json"), encoding="utf-8") as f:
            return (json.load(f) or {}).get("token", "") or ""
    except Exception:
        return ""


def _utf8_streams():
    """stdin/stdout/stderr 一律 UTF-8：Windows 默认 cp936 会把中文 JSON 弄成乱码。"""
    for name in ("stdin", "stdout", "stderr"):
        s = getattr(sys, name, None)
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


class OpsMCP:
    """一个 agent 接口插件的 MCP 面：读平台的操作目录，把调用转发给平台执行。"""

    def __init__(self, plugin_id, core_url=None, token=None, verbose=False, tool_name=DEFAULT_TOOL):
        self.plugin_id = plugin_id
        self.core = (core_url or CORE_URL).rstrip("/")
        self.token = load_token() if token is None else token
        self.verbose = verbose
        self.tool_name = tool_name

    # ── 平台 HTTP ─────────────────────────────────────────────
    def log(self, msg):
        print(f"[{self.plugin_id}:mcp] {msg}", file=sys.stderr, flush=True)

    def _request(self, method, path, payload=None, timeout=120):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["X-Plugin-Token"] = self.token
        req = urllib.request.Request(self.core + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                return json.loads(body)
            except Exception:
                raise RuntimeError(f"平台返回 HTTP {e.code}：{body[:300]}")

    def ops(self):
        """平台的操作目录（含 released 标记，随档位变）。"""
        return self._request("GET", "/api/ops?plugin=" + urllib.parse.quote(self.plugin_id)).get("ops") or []

    def released(self):
        return [o for o in self.ops() if o.get("released")]

    def level(self):
        try:
            return self._request("GET", "/api/permissions?plugin=" + urllib.parse.quote(self.plugin_id)).get("level", "")
        except Exception:
            return ""

    # ── 一个接口工具 ──────────────────────────────────────────
    @staticmethod
    def op_line(op):
        return f"{op.get('id', '')}（{op.get('label', '')}｜{op.get('op', '')}｜{LEVEL_CN.get(op.get('level'), op.get('level'))}档）"

    def tool(self):
        """清单里那**唯一**的接口工具：op 的可选值就是当前释放的操作。"""
        rel = self.released()
        lv = self.level()
        ids = [o.get("id", "") for o in rel]
        body = "\n".join("  · " + self.op_line(o) for o in rel) or "  （当前一档都没放开，去 8790 控制台调档）"
        return {
            "name": self.tool_name,
            "description": (
                "枢阁（笔记库）的软件操作接口 —— 所有具体操作的调用与权限释放都走这一个入口。\n"
                f"当前档位「{LEVEL_CN.get(lv, lv)}」，已释放 {len(rel)} 个操作：\n{body}\n"
                f"用法：op 填上面的 id，args 放该操作的参数；op=\"{LIST_WORDS[0]}\" 可随时重新取一次清单。"
                "档位不够的操作会被平台拒绝（回执里写明需要哪一档）。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ids + [LIST_WORDS[0]],
                           "description": "要执行的操作 id（只能填上面 describe 里列出的；"
                                          f"或 \"{LIST_WORDS[0]}\" 取清单）"},
                    "args": {"type": "object", "additionalProperties": True,
                             "description": "该操作的参数：{id} 这类路径参数直接放这里，"
                                            "GET 的其余参数进 query、其它请求进 JSON body"},
                },
                "required": ["op"],
                "additionalProperties": True,
            },
        }

    # ── 工具调用 ──────────────────────────────────────────────
    def call(self, name, args):
        if not isinstance(args, dict):
            args = {}
        if name != self.tool_name:
            return self._text(f"这个 MCP 面只有一个接口工具「{self.tool_name}」（不是 {name}）："
                              f"入参 {{op, args}}，op 取当前释放的操作 id。", err=True)

        op_id = (args.get("op") or "").strip()
        op_args = args.get("args")
        if not isinstance(op_args, dict):
            op_args = {}
        # 容错：模型常把参数摊平在同一层（op 之外多出来的键），一并当参数用
        for k, v in args.items():
            if k not in ("op", "args"):
                op_args.setdefault(k, v)

        if op_id.lower() in LIST_WORDS:
            rel = self.released()
            return self._text(f"当前档位「{LEVEL_CN.get(self.level(), self.level())}」，释放 {len(rel)} 个操作：\n"
                              + "\n".join("  · " + self.op_line(o) for o in rel))

        op = next((o for o in self.ops() if o.get("id") == op_id), None)
        if op is None:
            rel = self.released()
            return self._text(f"没有这个操作：{op_id}。当前释放的操作：" +
                              "、".join(o.get("id", "") for o in rel), err=True)

        r = self._request("POST", "/api/ops/call",
                          {"plugin": self.plugin_id, "op": op_id, "args": op_args})
        if r.get("denied"):
            need = LEVEL_CN.get(r.get("need"), r.get("need"))
            have = LEVEL_CN.get(r.get("have"), r.get("have"))
            rel = self.released()
            return self._text(f"平台拒绝：{op_id} 属于「{need}」档，当前 agent 是「{have}」档。"
                              f"要在 8790 控制台的 {self.plugin_id} 详情页调档后重试。"
                              f"现在能调的是：" + "、".join(o.get("id", "") for o in rel), err=True)
        if not r.get("ok"):
            return self._text(f"平台执行失败（HTTP {r.get('status')}）："
                              f"{json.dumps(r.get('result') if r.get('result') is not None else r.get('error'), ensure_ascii=False)}",
                              err=True)
        return self._text(json.dumps(r.get("result"), ensure_ascii=False, indent=2))

    # ── JSON-RPC ──────────────────────────────────────────────
    @staticmethod
    def _text(s, err=False):
        res = {"content": [{"type": "text", "text": s}]}
        if err:
            res["isError"] = True
        return res

    @staticmethod
    def _ok(mid, result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid, code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    def initialize(self, params):
        want = (params or {}).get("protocolVersion") or ""
        return {
            "protocolVersion": want if want in PROTOCOLS else FALLBACK_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "second-brain-" + self.plugin_id, "version": SERVER_VERSION},
            "instructions": f"枢阁平台的软件操作接口（{self.tool_name}）。"
                            "具体操作有哪些、能不能调，由控制台给这个 agent 的档位决定；"
                            "被拒绝的调用会说明需要哪一档。",
        }

    def handle(self, msg):
        """一条请求 → 一条响应；通知（无 id）返回 None。"""
        if not isinstance(msg, dict):
            return self._err(None, -32600, "无效请求：不是 JSON 对象")
        mid = msg.get("id")
        method = msg.get("method")
        if msg.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return self._err(mid, -32600, "无效请求：缺 jsonrpc=2.0 或 method")
        if method.startswith("notifications/"):
            return None
        try:
            if method == "initialize":
                return self._ok(mid, self.initialize(msg.get("params")))
            if method == "ping":
                return self._ok(mid, {})
            if method == "tools/list":
                return self._ok(mid, {"tools": [self.tool()]})
            if method == "tools/call":
                p = msg.get("params") or {}
                return self._ok(mid, self.call(p.get("name") or "", p.get("arguments")))
            return self._err(mid, -32601, f"不支持的方法：{method}")
        except Exception as e:
            if self.verbose:
                self.log(f"{method} 出错：{type(e).__name__}: {e}")
            return self._err(mid, -32603, f"{type(e).__name__}: {e}")

    # ── 主循环 ────────────────────────────────────────────────
    def run(self, argv=None):
        _utf8_streams()
        ap = argparse.ArgumentParser(prog=f"{self.plugin_id} --mcp")
        ap.add_argument("--id", default=self.plugin_id, help="身份（默认即本插件的 id）")
        ap.add_argument("--core", default=self.core, help="平台地址，默认 %s" % CORE_URL)
        ap.add_argument("--tool", default=self.tool_name, help="接口工具的对外名字，默认 %s" % DEFAULT_TOOL)
        ap.add_argument("--verbose", action="store_true", help="把每次出错也打到 stderr")
        a = ap.parse_args(argv)
        self.core = a.core.rstrip("/")
        self.tool_name = a.tool
        self.verbose = self.verbose or a.verbose

        self.log(f"MCP 面就绪：plugin={self.plugin_id} core={self.core} 接口工具={self.tool_name}"
                 "（清单与授权都在平台侧）")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception as e:
                self._write(self._err(None, -32700, f"JSON 解析失败：{e}"))
                continue
            resp = self.handle(msg)
            if resp is not None:
                self._write(resp)
        self.log("stdin 关闭 → 退出")
        return 0

    @staticmethod
    def _write(resp):
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def run_mcp(plugin_id, argv=None):
    """插件文件里 `--mcp` 分支的入口。"""
    return OpsMCP(plugin_id).run(argv)
