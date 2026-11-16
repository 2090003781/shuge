"""检查经代理返回的界面 HTML：API 调用是否都带 /notes/ 前缀。
用法：python tools/check_ui_paths.py [core_port]
"""
import re
import sys
import urllib.request

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8790
url = "http://127.0.0.1:%d/notes/" % port
with urllib.request.urlopen(url, timeout=20) as r:
    html = r.read().decode("utf-8", "replace")

print("页面:", url, "| 长度:", len(html))
i = html.find("API_BASE")
print("--- API_BASE 定义 ---")
print(html[i - 40:i + 220] if i >= 0 else "(没找到 API_BASE！)")

print("--- 所有 api( 调用 ---")
for m in re.finditer(r"await api\(([^;]{0,60})", html):
    print("   ", m.group(1).strip().replace("\n", " ")[:70])

print("--- 所有裸 fetch( 调用 ---")
for m in re.finditer(r"fetch\(([^;]{0,60})", html):
    print("   ", m.group(1).strip().replace("\n", " ")[:70])

print("--- 是否还有绝对 /api/ 残留 ---")
bad = [m.group(0) for m in re.finditer(r"['\"]/api/[a-z]+", html)]
print("   ", bad or "无")
