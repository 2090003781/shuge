"""检查界面托管：GET / → HTML。用法：python tools/check_ui.py [port]"""
import sys
import urllib.request

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8848
with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=10) as r:
    body = r.read().decode("utf-8", "replace")
    ctype = r.headers.get("Content-Type")
print("status:", r.status)
print("content-type:", ctype)
print("长度:", len(body))
print("含 ai-notes 标题:", "ai-notes" in body)
print("含编辑器元素:", 'id="editor"' in body)
print("含文件树元素:", 'id="tree"' in body)
print("含搜索框:", 'id="q"' in body)
