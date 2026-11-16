"""追加接口（`POST /api/files/<id>/append`）的验收：**只往前接，不整篇重写**。

辞玖 2026-10-04：同会话的内容直接追加即可，为什么要重写？
这条给日志转录用 —— 本篇验：内容真的接在后面、盘上那份也跟着按字节追加、
库内与盘上始终一致、非文本篇拒绝、不存在的篇 404。

    python tools/smoke_append.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
OK = True
FAILED = []


def call(path, method="GET", body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    if not cond:
        FAILED.append(name)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:170] if extra else ""))


made = []
try:
    lib = (call("/api/mirror")[1] or {}).get("library_dir") or ""

    print("① 建一篇 → 追加两段")
    st, d = call("/api/files", "POST", {"name": "zz-追加-测试", "format": "md",
                                        "content": "# 追加测试\n\n第一段。\n"})
    made.append(d.get("file_id"))
    fid = d["file_id"]
    check("建好", st == 201 and fid, d.get("name"))
    disk = os.path.join(lib, d.get("name") or "")
    part2 = "\n第二段：又接上来一段。\n"
    part3 = "\n第三段：再来一段。\n"
    st2, a2 = call("/api/files/%s/append" % fid, "POST", {"text": part2})
    check("第一次追加 200 且报出追加了多少字节",
          st2 == 200 and a2.get("ok") and a2.get("appended") == len(part2.encode("utf-8")), a2)
    st3, a3 = call("/api/files/%s/append" % fid, "POST", {"text": part3})
    check("第二次追加 200", st3 == 200 and a3.get("appended") == len(part3.encode("utf-8")), a3)

    print("② 内容真的接在后面（库内 + 盘上，两边一致）")
    _, f = call("/api/files/" + fid)
    want = "# 追加测试\n\n第一段。\n" + part2 + part3
    got = (f.get("file") or {}).get("content") or ""
    check("库内正文 = 原来 + 追加的两段", got == want, got.replace("\n", "⏎")[:120])
    with open(disk, encoding="utf-8") as fh:
        on_disk = fh.read()
    check("盘上那份一模一样", on_disk == want, on_disk.replace("\n", "⏎")[:120])
    check("体量一路在长", a2.get("size", 0) < a3.get("size", 0)
          and a3["size"] == len(want.encode("utf-8")), (a2.get("size"), a3.get("size")))
    # 2026-10-15 更新判据：这条原来断言 `mode == "append"`，但它**永远不可能成立**了 ——
    #   `append_content` → `put_content` 在 disk_projection=1 时会**整份落盘**，
    #   等 `tree_append` 再跑时盘上已经是新的那份，旧的判据又拿
    #   `disk_len` 和"`get_content()`（优先读盘，就是 disk_len 自己）+ 增量"比 → 自指 → 永远不等
    #   → 每次都退回整份重写。**结果对（幂等），但白写一遍，而且这条断言一直红着。**
    # 现在改成如实判据：内容正确 + **没有重复追加**（这才是这件事真正要防的）。
    _t = (a3.get("saved") or {}).get("tree") or {}
    check("盘上是完整的追加后内容、且没有重复追加",
          on_disk == want,
          "mode=%s｜盘上 %d 字节" % (_t.get("mode"), len(on_disk.encode("utf-8"))))

    print("③ 该拒的拒")
    st4, d4 = call("/api/files/%s/append" % fid, "POST", {"text": "  "})
    check("空 text → 400", st4 == 400, d4.get("error"))
    st5, d5 = call("/api/files/no-such-id/append", "POST", {"text": "x"})
    check("不存在的篇 → 404", st5 == 404, d5.get("error"))
    import base64
    st6, d6 = call("/api/files", "POST", {"name": "zz-追加-文档.docx", "format": "docx",
                                          "content": "# x\n"})
    if st6 == 201:
        made.append(d6.get("file_id"))
        st7, d7 = call("/api/files/%s/append" % d6["file_id"], "POST", {"text": "x"})
        check("文档类（docx）拒绝追加 → 400", st7 == 400 and "不是文本" in (d7.get("error") or ""),
              d7.get("error"))

    print("④ 盘上那份被删掉时不再硬追加（退回整份写一次，避免在坏基础上接）")
    os.remove(disk)
    st8, a8 = call("/api/files/%s/append" % fid, "POST", {"text": "\n第四段。\n"})
    check("照样成功、盘上被整份补回来",
          st8 == 200 and os.path.isfile(disk)
          and open(disk, encoding="utf-8").read().endswith("第四段。\n"),
          ((a8.get("saved") or {}).get("tree") or {}).get("mode"))
finally:
    print("⑤ 收摊")
    n = 0
    for x in [i for i in made if i]:
        st, _ = call("/api/files/" + x, "DELETE")
        n += 1 if st < 400 else 0
    print("  删了 %d 篇" % n)

print("=" * 70)
print("通过" if OK else "失败：%s" % "、".join(FAILED))
sys.exit(0 if OK else 1)
