"""走接口写库内容（辞玖 2026-09-25：库中文件等内容的写入要经过接口，不能绕道）。

  python scripts/brain_cli.py read   "vault/记忆/热缓存.md"
  python scripts/brain_cli.py write  "vault/记忆/热缓存.md" --writer task:日报 --mode merge --from-file x.md
  python scripts/brain_cli.py write  "vault/…" --mode replace --text "…" --allow-shrink
  python scripts/brain_cli.py create "vault/目录/新篇.md" --writer agent:dsh --text "# 标题"
  python scripts/brain_cli.py delete "vault/目录/旧篇.md" --writer agent:dsh

为什么要有它：agent / 定时任务 / 脚本以前是"直接写文件"——那等于绕过库的接口（没有前像、没有账本、
没有缩短闸）。这个客户端把同样的语义放到**接口那一层**：
  · 写 = `PUT /api/files/<id>`（服务端自带：前像、写入者标签、缩短闸、落库文件夹、镜像去 vault）
  · 建 = `POST /api/files`、删 = `DELETE /api/files/<id>`（同样带标签与前像）
库地址默认 http://127.0.0.1:8848，可用 AINOTES_URL 覆盖。写入者标签优先走 X-Writer（HTTP 头只能是 ASCII），
带中文的标签（例如 task:日报）**自动改走 body.writer**（create / write / delete 三条路都已接）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

LIB = (os.environ.get("AINOTES_URL") or "http://127.0.0.1:8848").rstrip("/")


def call(path, body=None, method="GET", writer="", allow_json_writer=False, timeout=600):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json"}
    if writer:
        if not allow_json_writer and not writer.isascii():
            allow_json_writer = True               # 带中文的标签自动改走 body（头只能是 ASCII）
        if allow_json_writer:                      # 中文标签：头只能是 ASCII → 走 body
            body = dict(body) if body is not None else {}
            body = dict(body, writer=writer)
            data = json.dumps(body).encode("utf-8")
        else:
            h["X-Writer"] = writer
    req = urllib.request.Request(LIB + path, data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.load(r)


def tree_index():
    """库内路径 → file_id（按树走一遍）"""
    _, d = call("/api/tree")
    out = {}

    def walk(n, path):
        path = path + [n.get("name", "")]
        for f in n.get("files", []):
            out["/".join([x for x in path if x] + [f["name"]])] = f["id"]
        for c in (n.get("children") or []):
            walk(c, path)

    walk(d["tree"], [])
    return out


def norm(p: str) -> str:
    p = (p or "").replace("\\", "/").strip("/")
    return p if p.startswith("vault/") or "/" in p else "vault/" + p


def folder_index():
    """库内文件夹路径 → id（服务端给的是带路径的扁平表）"""
    _, d = call("/api/folders")
    rows = d.get("folders") or d.get("items") or []
    return {r.get("path"): r.get("id") for r in rows if r.get("path") is not None}


def ensure_folder(parts, writer=""):
    """逐段建/找文件夹（vault 下的相对段），返回最里层的 folder_id"""
    idx = folder_index()
    cur = idx.get("vault") or idx.get("") or None
    acc = "vault"
    for seg in parts:
        acc = acc + "/" + seg
        if idx.get(acc):
            cur = idx[acc]
            continue
        _, fr = call("/api/folders", {"parent_id": cur, "name": seg}, "POST", writer=writer,
                     allow_json_writer=True)
        cur = fr.get("folder_id")
        idx[acc] = cur
    return cur


def find_id(path):
    idx = tree_index()
    key = norm(path)
    if key in idx:
        return idx[key]
    tail = "/" + key
    hit = [v for k, v in idx.items() if ("/" + k).endswith(tail)]
    return hit[0] if len(hit) == 1 else None


def read_text(path, fid=None):
    """按 id 读（走 /api/files/<id>）；这条最稳，不依赖路径形态"""
    if not fid:
        fid = find_id(path)
    if not fid:
        return ""
    _, d = call("/api/files/" + fid)
    return (d.get("file") or {}).get("content") or ""


def merge_lines(old, new):
    have = {ln.rstrip() for ln in old.splitlines()}
    add = [ln for ln in new.splitlines() if ln.rstrip() and ln.rstrip() not in have]
    if not add:
        return old
    out = old + ("" if (not old or old.endswith("\n")) else "\n") + "\n".join(add) + "\n"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("read", "write", "create", "delete"))
    ap.add_argument("path")
    ap.add_argument("--writer", default="tool:brain_cli")
    ap.add_argument("--mode", choices=("merge", "append", "replace"), default="merge")
    ap.add_argument("--text", default=None)
    ap.add_argument("--from-file", default=None)
    ap.add_argument("--allow-shrink", action="store_true")
    a = ap.parse_args()
    if a.cmd == "read":
        print(read_text(a.path))
        return 0

    if a.cmd in ("write", "create"):
        new = a.text if a.text is not None else (
            open(a.from_file, encoding="utf-8", errors="replace").read() if a.from_file
            else sys.stdin.read())
        if a.cmd == "create":
            segs = norm(a.path).split("/")
            name = segs[-1]
            fid_parent = ensure_folder(segs[1:-1], writer=a.writer)     # 去掉 vault 那一段
            st, d = call("/api/files", {"folder_id": fid_parent, "name": name, "content": new},
                         "POST", writer=a.writer, allow_json_writer=True)
            print(json.dumps({"ok": st in (200, 201), "file_id": d.get("file_id")},
                             ensure_ascii=False))
            return 0 if st in (200, 201) else 1
        fid = find_id(a.path)
        if not fid:
            print(json.dumps({"ok": False, "error": "库里没这篇（要新建用 create）：" + a.path},
                             ensure_ascii=False))
            return 2
        old = read_text(a.path, fid)
        out = merge_lines(old, new) if a.mode == "merge" else (
            old + ("" if (not old or old.endswith("\n")) else "\n") + new if a.mode == "append" else new)
        if out == old:
            print(json.dumps({"ok": True, "changed": False}, ensure_ascii=False))
            return 0
        body = {"content": out}
        if a.allow_shrink:
            body["allow_shrink"] = True
        try:
            st, d = call("/api/files/" + fid, body, "PUT", writer=a.writer,
                         allow_json_writer=True)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            print(json.dumps({"ok": False, "http": e.code, "resp": raw[:300]}, ensure_ascii=False))
            return 3
        print(json.dumps({"ok": d.get("ok") is not False, "bytes": len(out.encode("utf-8")),
                          "saved": (d.get("saved") or {}).get("chunks")}, ensure_ascii=False))
        return 0

    fid = find_id(a.path)
    if not fid:
        print(json.dumps({"ok": False, "error": "库里没这篇：" + a.path}, ensure_ascii=False))
        return 2
    st, d = call("/api/files/" + fid, None, "DELETE", writer=a.writer, allow_json_writer=True)
    print(json.dumps({"ok": bool(d.get("deleted")), "deleted": d.get("deleted")},
                     ensure_ascii=False))
    return 0 if d.get("deleted") else 1


if __name__ == "__main__":
    sys.exit(main())
