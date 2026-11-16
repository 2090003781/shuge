"""验证 core 的 /notes 反向代理（枢阁统一入口）。
用法：python tools/check_proxy.py [core_port]
"""
import json
import sys
import urllib.request

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8790
base = "http://127.0.0.1:%d" % port


def get(path, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


print("=== ① GET /notes （应 302 → /notes/）===")
try:
    req = urllib.request.Request(base + "/notes")
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
    with opener.open(req, timeout=10) as r:
        body = r.read()
        print("最终状态:", r.status, "| URL:", r.url, "| 长度:", len(body))
except Exception as e:
    print("请求异常:", e)

print("=== ② GET /notes/ （应返回 ai-notes 界面 HTML）===")
st, hd, body = get("/notes/")
txt = body.decode("utf-8", "replace")
print("status:", st, "| content-type:", hd.get("Content-Type"))
print("  含 ai-notes:", "ai-notes" in txt, "| 含编辑器:", 'id="editor"' in txt, "| 长度:", len(txt))

print("=== ③ GET /notes/api/tree （代理到 ai-notes 的库）===")
st, hd, body = get("/notes/api/tree")
d = json.loads(body.decode("utf-8"))
print("status:", st, "| ok:", d.get("ok"))
root = d.get("tree", {})
print("  顶层目录:", [c["name"] or "(根)" for c in (root.get("children") or [])])

print("=== ④ GET /notes/api/health ===")
st, hd, body = get("/notes/api/health")
d = json.loads(body.decode("utf-8"))
print("status:", st, "| files=%s chunks=%s" % (d["stats"]["files"], d["stats"]["chunks"]))

print("=== ⑤ 白名单（非 ai-notes 路径应 404）===")
st, hd, body = get("/notes/../etc/passwd")
print("status:", st, "(应 404)")

print("=== ⑥ 搜索（经代理）===")
st, hd, body = get("/notes/api/search?q=%E7%A7%81%E5%AF%86%E5%8A%A0%E9%94%81&limit=2")
d = json.loads(body.decode("utf-8"))
print("status:", st, "| 命中:", len(d.get("hits", [])),
      "|", (d["hits"][0]["name"] if d.get("hits") else ""))
