"""API 验收：逐个端点打一遍（含写路径与派生层联动）。

前置：先启动服务  python src/server.py --port 8848
用法：python tools/smoke_api.py [port]
"""
from __future__ import annotations

import json
import sys
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8848
BASE = "http://127.0.0.1:%d" % PORT


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def main() -> int:
    print("=" * 60)
    print("ai-notes API 验收 @", BASE)
    print("=" * 60)

    st, d = call("GET", "/api/health")
    print("[1] /api/health →", st, "| files=%s chunks=%s" % (d["stats"]["files"], d["stats"]["chunks"]))

    st, d = call("GET", "/api/tree")
    root = d["tree"]
    n_dir = len(root.get("children") or [])
    n_file = len(root.get("files") or [])
    print("[2] /api/tree →", st, "| 顶层目录 %d 个 / 顶层文件 %d 个" % (n_dir, n_file))
    for c in (root.get("children") or [])[:3]:
        print("      📁 %-24s 文件 %d" % (c["name"], len(c.get("files") or [])))

    # 挑一篇**真笔记只为读**（不写它 —— 冒烟测试不许动真数据）
    # 递归找，别只看顶层：正文现在都在 vault/… 下（2026-09-22 那 3 篇根文件挪进 second-brain-system/ 后，
    # 顶层直接挂的文件就没有了，只找一层的写法会拿到 None）
    def _walk(n, out):
        out.extend(n.get("files") or [])
        for ch in (n.get("children") or []):
            _walk(ch, out)
        return out

    _all = _walk(root, [])
    fid, fname = (_all[0]["id"], _all[0]["name"]) if _all else (None, None)
    if fid is None:
        raise SystemExit("库是空的，没法读一篇真笔记")

    st, d = call("GET", "/api/files/" + fid)
    text = d["file"]["content"]
    print("[3] GET /api/files/<id> →", st, "| %s (%d 字符，只读)" % (d["file"]["name"], len(text)))

    # 读写/建删验收全部用自建临时文件
    probe = "\n\n<!-- API 验收标记 " + "ok" + " -->\n"
    st, d = call("POST", "/api/files", {"name": "zz-API验收-读写.md",
                                        "content": "# 临时\n这是 API 建的文件。"})
    new_id = d.get("file_id")
    print("[4] POST /api/files →", st, "| 新文件 id=%s" % (new_id or "")[:8])

    st, d = call("PUT", "/api/files/" + str(new_id), {"content": "# 临时\n这是 API 建的文件。" + probe})
    print("[5] PUT /api/files/<id> →", st, "| 保存并重新索引 chunks=%s" % d.get("saved", {}).get("chunks"))

    st, d = call("GET", "/api/search?q=%E9%AA%8C%E6%94%B6%E6%A0%87%E8%AE%B0&mode=kw")
    print("[6] GET /api/search?mode=kw →", st, "| %d 命中（应能搜到刚写入的标记）" % len(d["hits"]))

    st, d = call("GET", "/api/search?q=%E4%B8%AD%E6%96%87%E5%85%A8%E6%96%87%E6%A3%80%E7%B4%A2%E4%B8%BA%E4%BB%80%E4%B9%88%E6%90%9C%E4%B8%8D%E5%88%B0&limit=3")
    print("[7] GET /api/search（hybrid） →", st)
    for h in d["hits"][:2]:
        print("      · %s [%s] rrf=%.4f" % (h["name"][:26], "+".join(h.get("channels", [])), h.get("rrf", 0)))

    st, d = call("DELETE", "/api/files/" + str(new_id))
    print("[8] DELETE /api/files/<id> →", st, "| deleted=%s" % (d.get("deleted") or "")[:8])

    st, d = call("GET", "/api/files/" + fid)
    same = d["file"]["content"] == text
    print("[9] 复查真笔记未被改动 →", same)

    st, d = call("GET", "/api/health")
    s = d["stats"]
    print("[10] 收尾 /api/health →", st, "| files=%s chunks=%s disk=%sB" % (s["files"], s["chunks"], s["disk_bytes"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
