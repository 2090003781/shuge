#!/usr/bin/env python3
"""笔记查找插件（独立进程）——原内核内模块 internal/modules/notesearch.go 的外移版。

工具：note.search
    args: query(必填) limit(默认10) scope(name|content|both 默认both)
    返回：[{path, name, kind, snippet}]（path 是 Vault 相对路径，正斜杠）

怎么被调到（2026-09-12 补的协议）：
    agent 发 kind=tool-call → 内核 bridge 先在内核模块里找，找不到就按**能力名 == 工具名**
    找到本插件（capabilities 里声明了 note.search）→ 经插件调用通道转发 →
    本插件执行 → 回执 → 内核把结果还给调用方。所以"能力声明"就是路由表，别写错。

Vault 根目录：优先 --vault / 环境变量 VAULT_ROOT，否则读内核 config.yaml 的 resources.vault.path。
排除目录与内核侧 internal/vault 的 ExcludePaths 保持一致（那份是权威；这里镜像一份，改动要同步）。
"""
import argparse
import os
import re
import signal
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))

from probe import Probe, auth_headers  # noqa: E402

CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:8790")
CORE_DIR = os.environ.get("CORE_DIR", os.path.dirname(HERE))
PLUGIN_ID = "plugin.note-search"
PARENT_CHECK_SECONDS = 5

# 与内层 internal/vault.ExcludePaths 一致（Vault 里的系统/私密/归档目录不进搜索）
EXCLUDE_PREFIXES = [".obsidian", ".trash", ".git", "个人/私密", "日志/reasonix-raw", "tmp"]
TEXT_EXT = {".md", ".txt", ".json", ".yaml", ".yml"}
HEAD_BYTES = 64 * 1024

running = True
_stats = {"calls": 0, "scanned": 0, "hits": 0, "errors": 0, "last_query": "", "last_at": 0.0, "vault": ""}


def handle_stop(signum, frame):
    global running
    print(f"[note-search] 收到信号 {signum}，优雅退出", flush=True)
    running = False


def _pid_alive(pid):
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


def http_post(path, payload, timeout=10):
    body = __import__("json").dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(CORE_URL + path, data=body,
                                 headers=auth_headers({"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return __import__("json").loads(r.read().decode("utf-8"))


def read_vault_root_from_config():
    """从内核 config.yaml 里取 resources.vault.path（不引入 YAML 依赖，只认这一行）"""
    cfg = os.path.join(CORE_DIR, "config.yaml")
    try:
        with open(cfg, encoding="utf-8") as f:
            in_vault = False
            for line in f:
                if re.match(r"^\s*vault:\s*$", line):
                    in_vault = True
                    continue
                if in_vault:
                    m = re.match(r'^\s*path:\s*["\']?([^"\'\s#]+)', line)
                    if m:
                        return m.group(1)
                    if re.match(r"^\S", line):  # 出了 vault 段
                        break
    except Exception as e:
        print(f"[note-search] 读 config.yaml 失败：{e}", flush=True)
    return ""


def excluded(rel_slash):
    for p in EXCLUDE_PREFIXES:
        if rel_slash == p or rel_slash.startswith(p + "/"):
            return True
    return False


def snippet_around(text, idx, half=40):
    start = max(0, idx - half)
    end = min(len(text), idx + half)
    cut = text.rfind("\n", start, idx)
    if cut >= 0:
        start = cut + 1
    cut = text.find("\n", idx, end)
    if cut >= 0:
        end = cut
    snip = text[start:end].strip()
    return (snip[:80] + "...") if len(snip) > 80 else snip


def search(query, limit=10, scope="both", root=None):
    root = root or _stats["vault"]
    ql = query.lower()
    results = []
    visited = 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        # 目录级排除（同时阻止继续下探）
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and not excluded((rel_dir + "/" + d).lstrip("/"))]
        if rel_dir and excluded(rel_dir):
            continue
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in TEXT_EXT:
                continue
            if len(results) >= limit:
                return results, visited
            rel = (rel_dir + "/" + fn).lstrip("/")
            if excluded(rel):
                continue
            visited += 1
            name_hit = ql in fn.lower()
            content_hit, snippet = False, ""
            if scope != "name":
                try:
                    with open(os.path.join(dirpath, fn), encoding="utf-8", errors="ignore") as f:
                        head = f.read(HEAD_BYTES)
                    low = head.lower()
                    pos = low.find(ql)
                    if pos >= 0:
                        content_hit = True
                        snippet = snippet_around(head, pos)
                except Exception:
                    pass
            hit = (scope == "name" and name_hit) or \
                  (scope == "content" and content_hit) or \
                  (scope == "both" and (name_hit or content_hit))
            if hit:
                kind = "content"
                if name_hit:
                    kind = "name+content" if content_hit else "name"
                results.append({"path": rel, "name": fn, "kind": kind, "snippet": snippet})
    return results, visited


def note_search(args):
    query = str(args.get("query") or "").strip()
    if not query:
        _stats["errors"] += 1
        return {"ok": False, "error": "note.search requires query"}
    try:
        limit = int(args.get("limit") or 10)
    except Exception:
        limit = 10
    scope = str(args.get("scope") or "both")
    try:
        results, visited = search(query, limit, scope)
    except Exception as e:
        _stats["errors"] += 1
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    _stats["calls"] += 1
    _stats["scanned"] += visited
    _stats["hits"] += len(results)
    _stats["last_query"] = query
    _stats["last_at"] = time.time()
    print(f"[note-search] query={query!r} scope={scope} 扫 {visited} 个文件 → 命中 {len(results)}", flush=True)
    return results  # 直接返回数组：内核按 ToolResult.Data 透传给调用方


def status(_args=None):
    out = dict(_stats)
    out["vault"] = _stats["vault"]
    out["vault_exists"] = os.path.isdir(_stats["vault"]) if _stats["vault"] else False
    out["last_age_s"] = round(time.time() - _stats["last_at"], 1) if _stats["last_at"] else None
    return out


def _probe_stats():
    return {"calls": _stats["calls"], "hits": _stats["hits"], "errors": _stats["errors"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default=PLUGIN_ID)
    ap.add_argument("--vault", default=os.environ.get("VAULT_ROOT", ""))
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    root = args.vault or read_vault_root_from_config()
    if not root:
        print("[note-search] 警告：拿不到 Vault 根目录（--vault / VAULT_ROOT / config.yaml）", flush=True)
    _stats["vault"] = os.path.abspath(root) if root else ""

    try:
        http_post("/api/bridge/plugins/register", {
            "id": args.id, "name": "笔记查找",
            "description": "在 Vault 笔记库里按文件名/内容关键词搜索，返回命中路径与上下文片段；"
                           "工具名 note.search（参数 query / limit / scope=name|content|both）。"
                           "只搜 .md/.txt/.json/.yaml，自动跳过 .trash/.git/.obsidian 等系统目录。",
            "capabilities": ["note.search"],   # ← 能力名 == 工具名，内核按它路由
            "produces": [], "consumes": [],
        })
        print(f"[note-search] 注册成功 {args.id}（工具 note.search，vault={_stats['vault']}）", flush=True)
    except Exception as e:
        print(f"[note-search] 注册失败（平台未就绪）: {e}", flush=True)

    probe = Probe(args.id, handlers={"note.search": note_search, "note.status": status},
                  stats_provider=_probe_stats, core_url=CORE_URL)
    probe.start()

    parent_pid = os.getppid()
    next_parent = time.time() + PARENT_CHECK_SECONDS
    while running:
        time.sleep(0.5)
        now = time.time()
        if now >= next_parent:
            next_parent = now + PARENT_CHECK_SECONDS
            if not _pid_alive(parent_pid):
                print("[note-search] 父进程(core)已退出 → 自行退出，避免孤儿", flush=True)
                break

    try:
        probe.stop()
    except Exception:
        pass
    print(f"[note-search] 正常退出（{_stats['calls']} 次查询 / {_stats['hits']} 条命中）", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
