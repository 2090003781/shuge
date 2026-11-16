"""注入包"从库取内容"的现场验证（需要平台在跑）。

证法很硬：
  ① 造一个**只在库里、磁盘上没有**的文件 → 包能取到它 ⇒ 内容确实来自库
  ② 把库停掉 → 同一个包条目仍能取到内容 ⇒ 磁盘兜底还在
自建自清。
"""
import json
import sys
import urllib.error
import urllib.request

CORE = "http://127.0.0.1:8790"
NOTES = "http://127.0.0.1:8848"
ok_all = True
FID = None


def post(url, obj, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:160] if extra else ""))


def ctx(tool, args):
    """走插件的正式工具通道"""
    r = post(CORE + "/api/bridge/plugins/call",
             {"plugin": "plugin.context-pack", "tool": tool, "args": args or {}, "timeoutMs": 40000})
    if not r.get("ok"):
        raise RuntimeError(r.get("error") or "插件没回执")
    return r.get("data")


LIB_PATH = "vault/zz-库专用-注入包测试.md"
BODY = "# 只在库里的一篇\n\nZZLIBONLY 这段文字磁盘上不存在。\n"

try:
    print("① 造一篇「只在库里」的文件（磁盘上没有）")
    folders = get(NOTES + "/api/folders")["folders"]
    vault = next(f for f in folders if f["path"] == "vault")
    d = post(NOTES + "/api/files", {"folder_id": vault["id"], "name": "zz-库专用-注入包测试.md", "content": BODY})
    FID = d["file_id"]
    disk = r"D:\个人数据\辞玖\zz-库专用-注入包测试.md"
    import os
    check("已入库", bool(FID), FID)
    check("磁盘上确实没有同名文件", not os.path.exists(disk), disk)

    print("② 让注入包预览这个库内路径（应当取到正文）")
    data = ctx("pack.preview", {"files": [LIB_PATH]})
    files = data.get("files") or []
    hit = next((f for f in files if LIB_PATH.split("/")[-1] in json.dumps(f, ensure_ascii=False)), None)
    check("插件认得这个条目", bool(hit), files[:2])
    head = json.dumps(data, ensure_ascii=False)
    check("返回了库里的正文（ZZLIBONLY）", "ZZLIBONLY" in head, head[:160])

    print("③ 老写法（不带 vault/ 前缀）也要能取到")
    data2 = ctx("pack.preview", {"files": ["zz-库专用-注入包测试.md"]})
    check("老路径也能取到库内容", "ZZLIBONLY" in json.dumps(data2, ensure_ascii=False),
          json.dumps(data2, ensure_ascii=False)[:120])

    print("④ 把库停掉 → 磁盘兜底（用一篇两边都有的老条目）")
    post(CORE + "/api/notes/stop", {})
    import time
    time.sleep(2)
    legacy = "FORMAT-数据格式规范-v0.4.md"
    try:
        data3 = ctx("pack.preview", {"files": ["second-brain-system/产品化/" + legacy]})
        txt = json.dumps(data3, ensure_ascii=False)
        check("库停着时仍能取到内容（走磁盘）", "FORMAT" in txt and data3.get("files"),
              txt[:140])
    finally:
        post(CORE + "/api/notes/start", {})
        time.sleep(6)
finally:
    print("⑤ 清场")
    if FID:
        req = urllib.request.Request(NOTES + "/api/files/" + FID, method="DELETE")
        try:
            urllib.request.urlopen(req, timeout=30)
        except Exception as e:
            print("   删除失败：", e)
    try:
        h = get(NOTES + "/api/health")
        check("测试文件已删除", h["stats"]["files"] == 1162, h["stats"]["files"])
    except Exception as e:
        check("库已恢复", False, e)

print("\n========  结论：%s" % ("全部通过" if ok_all else "有失败项"))
sys.exit(0 if ok_all else 1)
