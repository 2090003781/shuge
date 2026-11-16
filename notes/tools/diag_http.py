"""诊断：模拟浏览器请求，打印状态行与全部响应头（排查"浏览器打不开"）。
用法：python tools/diag_http.py [port]
"""
import sys
import urllib.request

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8848
url = "http://127.0.0.1:%d/" % port
print("请求:", url)
req = urllib.request.Request(url, headers={
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
    "Accept": "text/html,application/xhtml+xml",
})
try:
    with urllib.request.urlopen(req, timeout=8) as r:
        print("状态行:", r.status, r.reason, "| HTTP 版本:", r.version)
        print("--- 响应头 ---")
        for k, v in r.headers.items():
            print("  %s: %s" % (k, v))
        body = r.read()
        print("--- 正文 ---")
        print("  长度:", len(body))
        txt = body.decode("utf-8", "replace")
        print("  首 80 字:", txt[:80].replace("\n", " "))
        print("  结尾 60 字:", txt[-60:].replace("\n", " "))
except Exception as e:
    print("❌ 请求失败:", type(e).__name__, e)
