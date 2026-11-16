"""agent 接口插件的 MCP 面自测（平台侧）。

  python scripts/smoke_ops_mcp.py

MCP 面就是 plugins/lib/ops_mcp.py，由 agent 宿主（DSH 的 dsh-mcp-client）用 stdio 拉起：
  python plugins/agent_plugin.py --agent dsh --mcp
这里就当一次那个宿主：握手 → 取清单 → 调操作，顺便验三件要紧的事——
  ① 对外只有**一个接口工具**（不是把 31 个操作拆成 31 个工具）；
  ② 清单与授权都在平台侧：改档不用重启 MCP 进程，接口工具立刻跟着变；
  ③ 越权调用被平台拒，模型看到的是明确的失败（还告诉你现在能调什么），而不是假的成功。
"""

import os as _os, sys as _sys          # 门禁用（2026-09-30 体检）

if "--yes" not in _sys.argv and _os.environ.get("COREHOST_ALLOW_DESTRUCTIVE") != "1":
    print(__doc__ or "")
    print("\n【门禁】%s 会真的动到**运行中的枢阁 / 库文件夹**（这个脚本忽略参数，--help 也照跑）。"
          % _os.path.basename(__file__))
    print("       确认要跑：python %s --yes（或设 COREHOST_ALLOW_DESTRUCTIVE=1）"
          % _os.path.basename(__file__))
    _sys.exit(2)
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

CORE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("CORE_URL", "http://127.0.0.1:8790")
PLUGIN = "plugin.dsh"
TOOL = "brain_ops"
OK = True


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:160] if extra else ""))


def api(path, body=None, method="GET"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:200]}


PERM_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "plugins", "permissions.json")


def snapshot_perms():
    """按字节快照权限文件：这个自测会**真的改档位**，跑完必须原样还回去。

    为什么快照整份文件而不是只记档位：测试若（将来）加了别的键，只还原档位会留下垃圾。
    为什么还要父进程兜底（见 ai-notes/tools/smoke_brain_plugin.py）：子进程被超时杀掉时，
    这里的 finally 根本跑不到（2026-10-04 辞玖的 trust 就是这么被回退成 write 的）。
    """
    try:
        with open(PERM_FILE, "rb") as f:
            return f.read()
    except OSError:
        return None


def restore_perms(blob):
    if blob is None:
        return False
    try:
        os.makedirs(os.path.dirname(PERM_FILE), exist_ok=True)
        tmp = PERM_FILE + ".restore-tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, PERM_FILE)
        return True
    except OSError:
        return False


def set_level(level=None):
    if level:
        api("/api/permissions/set", {"plugin": PLUGIN, "level": level}, "POST")
    _, d = api("/api/permissions?plugin=" + PLUGIN)
    return d.get("level"), d.get("released"), d.get("total")


def released_ids():
    _, d = api("/api/ops?plugin=" + PLUGIN)
    return sorted(o["id"] for o in d["ops"] if o["released"])


class Host:
    """冒充一次 MCP 宿主：一行一条 JSON-RPC。"""

    def __init__(self):
        env = dict(os.environ)
        env["CORE_URL"] = BASE
        self.p = subprocess.Popen(
            # 2026-10-09 修：原来写 plugins/dsh_plugin.py（2026-10-05 模板化时已归档）→ 体检必红
            [sys.executable, "plugins/agent_plugin.py", "--agent", "dsh", "--mcp"],
            cwd=CORE_DIR, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=NO_WINDOW)
        self.seq = 0

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.seq += 1
            msg["id"] = self.seq
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        return msg.get("id")

    def recv(self):
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("MCP 进程没有输出就退出了（stderr: %s）" % (self.stderr() or "")[-300:])
        return json.loads(line)

    def call(self, method, params=None):
        mid = self.send(method, params)
        r = self.recv()
        assert r.get("id") == mid, (mid, r)
        return r

    def invoke(self, name, arguments=None):
        r = self.call("tools/call", {"name": name, "arguments": arguments or {}})
        res = r.get("result") or {}
        text = "".join(c.get("text", "") for c in res.get("content") or [])
        return bool(res.get("isError")), text

    def op(self, op_id, args=None, flat=None):
        payload = {"op": op_id}
        if args is not None:
            payload["args"] = args
        if flat:
            payload.update(flat)
        return self.invoke(TOOL, payload)

    def tools(self):
        return (self.call("tools/list").get("result") or {}).get("tools") or []

    def stderr(self):
        try:
            return self.p.stderr.read()
        except Exception:
            return ""

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            return self.p.wait(timeout=15)
        except Exception:
            self.p.kill()
            return None


orig_level = None
orig_perms = snapshot_perms()          # 跑之前按字节留底（这个自测会真改档位）
h = None
try:
    orig_level, orig_released, total = set_level()
    print("① 握手（MCP 宿主视角）")
    h = Host()
    r = h.call("initialize", {"protocolVersion": "2025-11-25",
                              "capabilities": {}, "clientInfo": {"name": "smoke", "version": "0"}})
    info = r.get("result") or {}
    check("回同一个协议版本", info.get("protocolVersion") == "2025-11-25", info.get("protocolVersion"))
    check("声明了 tools 能力", "tools" in (info.get("capabilities") or {}), info.get("capabilities"))
    check("有 serverInfo.name", bool((info.get("serverInfo") or {}).get("name")),
          (info.get("serverInfo") or {}).get("name"))

    h.send("notifications/initialized", notify=True)
    mid = h.send("ping")
    r = h.recv()
    check("通知不回包（下一条就是 ping 的响应）", r.get("id") == mid and r.get("result") == {}, r)

    r = h.call("no/such-method")
    check("未知方法回 -32601", (r.get("error") or {}).get("code") == -32601, r.get("error"))

    print("② 对外只有**一个接口工具**（当前档 %s，释放 %s/%s）" % (orig_level, orig_released, total))
    tools = h.tools()
    check("tools/list 只有 1 个工具", len(tools) == 1, [t.get("name") for t in tools])
    t = tools[0] if tools else {}
    check("它叫 %s" % TOOL, t.get("name") == TOOL, t.get("name"))
    sch = t.get("inputSchema") or {}
    check("入参是 {op, args}（op 必填）", sch.get("required") == ["op"] and
          set((sch.get("properties") or {}).keys()) >= {"op", "args"}, list((sch.get("properties") or {}).keys()))
    enum = (sch.get("properties") or {}).get("op", {}).get("enum") or []
    check("op 的可选值 = 当前释放的 %d 个操作（+list）" % orig_released,
          sorted(x for x in enum if x != "list") == released_ids() and "list" in enum, len(enum))
    desc = t.get("description") or ""
    check("describe 里写清档位与释放条数", f"已释放 {orig_released} 个操作" in desc, desc[:80])
    check("describe 里每个操作一行（id + 说明 + 对应接口）",
          all(o in desc for o in released_ids()[:3]) and "GET /api/health" in desc)
    unrel = [o["id"] for o in api("/api/ops?plugin=" + PLUGIN)[1]["ops"] if not o["released"]]
    if unrel:
        check("未释放的操作不在可选值里（授权在平台）", all(u not in enum for u in unrel),
              "未释放 %d 条，如 %s" % (len(unrel), unrel[:3]))
    else:
        check("这一档放开了全部 %d 条（未释放 0 条，故无可验的隐藏项）" % total,
              len(released_ids()) == total)

    print("③ 真调（走这一个入口）")
    err, text = h.op("list")
    check("op=list 能现取清单", err is False and text.count("brain.") >= orig_released,
          "%d 行" % text.count("brain."))

    err, text = h.op("brain.health")
    check("brain.health 不是错误", err is False, text[:80])
    try:
        body = json.loads(text)
        check("回的是库的健康数据", body.get("ok") is True and body.get("stats", {}).get("files", 0) > 0,
              "files=%s" % body.get("stats", {}).get("files"))
    except Exception as e:
        check("回的是库的健康数据", False, "解析失败 %s / %s" % (e, text[:80]))

    err, text = h.op("brain.search", {"q": "契约", "limit": 1})
    check("brain.search（args 里带参数）跑通", err is False and "hits" in text, text[:80])
    err, text = h.op("brain.search", flat={"q": "契约", "limit": 1})
    check("参数摊平在同一层也认（容错）", err is False and "hits" in text)

    err, text = h.op("brain.nope")
    check("没有的操作是明确失败", err is True and "没有这个操作" in text, text[:100])
    err, text = h.invoke("brain_search", {"op": "brain.health"})
    check("调错工具名会被点破（只有一个入口）", err is True and TOOL in text, text[:100])

    print("④ 改档 → 接口实时跟着变（MCP 进程不重启），越权被平台拒")
    lv, rel, _ = set_level("read")
    tools = h.tools()
    enum2 = (((tools[0].get("inputSchema") or {}).get("properties") or {}).get("op") or {}).get("enum") or []
    check("降到「只读」后 op 可选值缩到 %d 个" % rel, len([x for x in enum2 if x != "list"]) == rel, len(enum2))
    check("写操作从可选值里消失", "brain.create" not in enum2)
    err, text = h.op("brain.health")
    check("只读操作仍可用", err is False)
    err, text = h.op("brain.create", {"path": "smoke/mcp-越权.md", "content": "x"})
    check("越权被拒且说明档位", err is True and "可写" in text and "只读" in text, text[:120])
    tail = text.split("现在能调的是", 1)[1] if "现在能调的是" in text else ""
    check("拒绝回执里告诉它现在能调什么", "brain.health" in tail and "brain.create" not in tail, tail[:100])

    lv, rel, _ = set_level(orig_level or "write")
    tools = h.tools()
    enum3 = (((tools[0].get("inputSchema") or {}).get("properties") or {}).get("op") or {}).get("enum") or []
    check("恢复档位后可选值回到 %d 个" % rel, len([x for x in enum3 if x != "list"]) == rel, len(enum3))

    print("⑤ 收尾")
    t0 = time.time()
    code = h.close()
    check("stdin 关闭后自己退出（退出码 0）", code == 0, "code=%s 用时 %.1fs" % (code, time.time() - t0))
    check("stderr 只有日志、没有异常", "Traceback" not in (h.stderr() or ""))
    h = None
finally:
    if h is not None:
        h.close()
    if orig_level:
        api("/api/permissions/set", {"plugin": PLUGIN, "level": orig_level}, "POST")
    if restore_perms(orig_perms):
        print("     权限文件已按字节还原（%s）" % PERM_FILE)

print("=" * 60)
print("MCP 面自测：%s" % ("全部通过" if OK else "有失败"))
sys.exit(0 if OK else 1)
