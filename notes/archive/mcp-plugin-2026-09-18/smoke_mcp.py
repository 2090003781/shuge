"""MCP 插件验收：握手、工具清单、权限（哪些暴露 / 哪些要 confirm）、真调一次读写。

插件在 agent/ 下：agent/server.py（stdio MCP）+ agent/mcp_tools.py（工具清单）+ agent/config.json（权限）。
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(HERE, "agent", "server.py")
BASE = "http://127.0.0.1:8848"
OK = True
MADE = []


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:160] if extra else ""))


def call(path, method="GET", body=None, timeout=300):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class Mcp:
    """一个极简 MCP 客户端：写一行 JSON、读一行 JSON（读用线程 + 超时，别把测试挂死）"""

    def __init__(self, config_path=None):
        env = dict(os.environ)
        if config_path:
            env["AI_NOTES_MCP_CONFIG"] = config_path
        self.p = subprocess.Popen([sys.executable, SERVER], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", env=env)
        self.q = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.n = 0

    def _pump(self):
        for line in self.p.stdout:
            self.q.put(line)

    def send(self, method, params=None, notify=False):
        self.n += 1
        msg = {"jsonrpc": "2.0", "method": method}
        if not notify:
            msg["id"] = self.n
        if params is not None:
            msg["params"] = params
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        if notify:
            return None
        try:
            return json.loads(self.q.get(timeout=60))
        except queue.Empty:
            return {"error": {"message": "超时没回"}}

    def tool(self, name, args=None):
        r = self.send("tools/call", {"name": name, "arguments": args or {}})
        res = (r or {}).get("result") or {}
        txt = "".join(c.get("text") or "" for c in (res.get("content") or []))
        return bool(res.get("isError")), txt

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.terminate()
            self.p.wait(timeout=10)
        except Exception:
            try:
                self.p.kill()
            except Exception:
                pass


BEFORE = call("/api/health")[1]["stats"]["files"]
c1 = c2 = None
try:
    print("① 握手与工具清单（默认权限：read + write）")
    c1 = Mcp()
    init = c1.send("initialize", {"protocolVersion": "2024-11-05",
                                  "clientInfo": {"name": "smoke", "version": "1"}})
    check("initialize 回话", (init.get("result") or {}).get("serverInfo", {}).get("name") == "ai-notes",
          init)
    check("声明了 tools 能力", "tools" in ((init.get("result") or {}).get("capabilities") or {}))
    check("协议版本按客户端要的回（2025-06-18）",
          (c1.send("initialize", {"protocolVersion": "2025-06-18"}).get("result") or {})
          .get("protocolVersion") == "2025-06-18")
    check("要了个不认识的版本 → 回我们最新的",
          (c1.send("initialize", {"protocolVersion": "1999-01-01"}).get("result") or {})
          .get("protocolVersion") == "2025-11-25")
    c1.send("notifications/cancelled", {"requestId": 999}, notify=True)
    check("不认识的通知不回应、后续照常",
          (c1.send("ping").get("result") is not None))
    c1.send("notifications/initialized", notify=True)
    lst = c1.send("tools/list")
    tools = (lst.get("result") or {}).get("tools") or []
    names = [t["name"] for t in tools]
    check("工具清单非空", len(tools) >= 15, len(tools))
    check("只读与写工具都在", "search_notes" in names and "create_note" in names, names[:6])
    check("危险工具默认不暴露", "delete_note" not in names and "set_note_visibility" not in names)
    check("每个工具都有入参 schema", all(isinstance(t.get("inputSchema"), dict) for t in tools))
    sr = [t for t in tools if t["name"] == "set_note_rag"][0]
    check("需要确认的工具在描述里写明了", "confirm=true" in sr["description"], sr["description"][-40:])

    print("② 真调一次只读工具")
    err, txt = c1.tool("library_health")
    check("library_health 成功", not err and '"files"' in txt, txt[:90])
    err, txt = c1.tool("list_tree")
    check("list_tree 成功", not err and len(txt) > 100, len(txt))
    err, txt = c1.tool("convert_engines")
    check("convert_engines 成功", not err and "pandoc" in txt, txt[:90])

    print("③ 通过插件建 / 改 / 搜一篇")
    err, txt = c1.tool("create_note", {"name": "zz-MCP-测试.md", "content": "# MCP 冒烟\n\n第一次写入。\n"})
    check("create_note 成功", not err, txt[:120])
    fid = (json.loads(txt).get("file_id") if not err else None)
    MADE.append(fid)
    check("返回了 file_id", bool(fid), fid)
    err, txt = c1.tool("update_note", {"id": fid, "content": "# MCP 冒烟\n\n第二次写入，带一个独特词 MCPPROBE。\n"})
    check("update_note 成功", not err, txt[:120])
    got = call("/api/files/" + fid)[1]["file"]["content"]
    check("正文真的改了", "MCPPROBE" in got, got[:60])
    time.sleep(0.5)
    err, txt = c1.tool("search_notes", {"q": "MCPPROBE"})
    check("改完立刻能搜到（插件 → 库 → 索引一条龙）", (not err) and fid in txt, txt[:140])

    print("④ 权限：要确认的必须带 confirm")
    err, txt = c1.tool("set_note_rag", {"id": fid, "on": False})
    check("不带 confirm 被拦下", err and "confirm" in txt, txt[:110])
    err, txt = c1.tool("set_note_rag", {"id": fid, "on": False, "confirm": True})
    check("带 confirm 就执行", (not err) and '"rag": "off"' in txt.replace(" ", " "), txt[:110])
    c1.tool("set_note_rag", {"id": fid, "on": True, "confirm": True})

    print("⑤ 危险档：默认不给，配置里放开后仍要 confirm")
    err, txt = c1.tool("delete_note", {"id": fid, "confirm": True})
    check("默认配置下 delete_note 不在工具里", err and "没有这个工具" in txt, txt[:110])
    cfg = os.path.join(HERE, "data", "_mcp_cfg_test.json")
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump({"base_url": BASE, "expose_tiers": ["read", "write", "danger"],
                   "require_confirm": [], "disabled_tools": []}, f, ensure_ascii=False)
    c2 = Mcp(cfg)
    c2.send("initialize", {"protocolVersion": "2024-11-05"})
    c2.send("notifications/initialized", notify=True)
    names2 = [t["name"] for t in ((c2.send("tools/list").get("result") or {}).get("tools") or [])]
    check("放开危险档后 delete_note 出现", "delete_note" in names2, names2[-4:])
    err, txt = c2.tool("delete_note", {"id": fid})
    check("危险操作不带 confirm 仍然被拦", err and "confirm" in txt, txt[:110])
    err, txt = c2.tool("delete_note", {"id": fid, "confirm": True})
    check("确认后确实删掉了", not err, txt[:110])
    MADE.remove(fid)
    check("库里也真的没了", call("/api/files/" + fid)[0] == 404)
    print("⑥ 权限管理（agent/perm.py / server.py --print-perms）")
    def perms(args, cfg_path=None):
        cmd = [sys.executable, os.path.join(HERE, "agent", "perm.py")]
        if cfg_path:
            cmd += ["--config", cfg_path]
        return subprocess.run(cmd + args, capture_output=True, text=True,
                              encoding="utf-8", timeout=60)

    r = perms(["--json"])
    j = json.loads(r.stdout)
    by = {t["name"]: t for t in j["tools"]}
    check("默认：只读与写工具暴露、危险档不暴露",
          by["search_notes"]["exposed"] and by["create_note"]["exposed"]
          and not by["delete_note"]["exposed"], j["expose_tiers"])
    check("默认：set_note_rag / reindex 要确认",
          by["set_note_rag"]["confirm"] and by["reindex"]["confirm"])
    check("默认：危险档一律要确认（哪怕档还没开）", by["delete_note"]["confirm"])
    check("暴露给 agent 的工具数 = 19", sum(1 for t in j["tools"] if t["exposed"]) == 19)
    r = subprocess.run([sys.executable, os.path.join(HERE, "agent", "server.py"),
                        "--print-perms"], capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
    check("插件自己也能打权限表", "delete_note" in r.stdout and "危险" in r.stdout, r.stdout[:80])

    cfg2 = os.path.join(HERE, "data", "_mcp_cfg_live.json")
    with open(cfg2, "w", encoding="utf-8") as f:
        json.dump({"base_url": BASE, "expose_tiers": ["read", "write"],
                   "require_confirm": [], "disabled_tools": []}, f, ensure_ascii=False)
    c3 = Mcp(cfg2)
    c3.send("initialize", {"protocolVersion": "2024-11-05"})
    c3.send("notifications/initialized", notify=True)
    n_before = [t["name"] for t in ((c3.send("tools/list").get("result") or {}).get("tools") or [])]
    check("起服务时 search_notes 在", "search_notes" in n_before, len(n_before))
    perms(["hide", "search_notes"], cfg2)
    n_after = [t["name"] for t in ((c3.send("tools/list").get("result") or {}).get("tools") or [])]
    check("改完配置立刻生效（不用重启客户端）", "search_notes" not in n_after
          and len(n_after) == len(n_before) - 1, "%d → %d" % (len(n_before), len(n_after)))
    perms(["expose", "+danger"], cfg2)
    n3 = [t["name"] for t in ((c3.send("tools/list").get("result") or {}).get("tools") or [])]
    check("放开 danger 档后危险工具出现", "delete_note" in n3, n3[-3:])
    perms(["require", "convert_note"], cfg2)
    desc = [t["description"] for t in ((c3.send("tools/list").get("result") or {}).get("tools") or [])
            if t["name"] == "convert_note"][0]
    check("新加的确认立刻体现在描述里", "confirm=true" in desc, desc[-30:])
    c3.close()
    os.remove(cfg2)

finally:
    print("⑦ 清场")
    for x in MADE:
        if x:
            call("/api/files/" + x, "DELETE")
    for p in (os.path.join(HERE, "data", "_mcp_cfg_test.json"),):
        try:
            os.remove(p)
        except Exception:
            pass
    for c in (c1, c2, locals().get("c3")):
        if c:
            c.close()
    import sqlite3
    conn = sqlite3.connect(os.path.join(HERE, "data", "index.db"))
    conn.execute("DELETE FROM tombstones WHERE display LIKE 'zz-%'")
    conn.commit()
    conn.close()
    after = call("/api/health")[1]["stats"]["files"]
    check("文件数复原", after == BEFORE, "%s → %s" % (BEFORE, after))

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
