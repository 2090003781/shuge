"""CORS 验收：模拟来自 3080 的跨源请求，确认响应头正确。
用法：python tools/check_cors.py [port]
"""
import sys
import urllib.request

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8848
origin = "http://127.0.0.1:3080"

print("=== ① 跨源 GET / （带 Origin 头）===")
req = urllib.request.Request("http://127.0.0.1:%d/" % port, headers={"Origin": origin})
with urllib.request.urlopen(req, timeout=8) as r:
    print("status:", r.status)
    for k, v in r.headers.items():
        if k.lower().startswith(("access-control", "vary", "content-type")):
            print("  %s: %s" % (k, v))
    ok = r.headers.get("Access-Control-Allow-Origin") == origin
    print("→ Access-Control-Allow-Origin 匹配来源:", ok)

print("=== ② 跨源 GET /api/tree ===")
req = urllib.request.Request("http://127.0.0.1:%d/api/tree" % port, headers={"Origin": origin})
with urllib.request.urlopen(req, timeout=8) as r:
    print("status:", r.status, "| ACAO:", r.headers.get("Access-Control-Allow-Origin"))

print("=== ③ 预检 OPTIONS ===")
req = urllib.request.Request("http://127.0.0.1:%d/api/files/x" % port, method="OPTIONS",
                             headers={"Origin": origin, "Access-Control-Request-Method": "PUT"})
with urllib.request.urlopen(req, timeout=8) as r:
    print("status:", r.status, "| allow-methods:", r.headers.get("Access-Control-Allow-Methods"))
