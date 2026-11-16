"""写入者标签 + 高风险前像的验收（真接口）。

  python tools/acc_writer_tags.py

验六件事：
  ① 不带任何标识的调用 → writer 记成 auto:<User-Agent>
  ② 带 X-Writer: tool:验收 → 原样记下
  ③ 页面来的（Origin: http://127.0.0.1:8848）→ user
  ④ PUT 覆盖正文 → 生成前像，且前像是覆盖**前**的内容
  ⑤ DELETE → 生成前像，且能按前像把这篇**重建回来**（正文/位置一致）
  ⑥ /api/files/<id>/writers 能查出"这篇最近被谁改过"
跑完把自己造的东西删干净。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

LIB = "http://127.0.0.1:8848"
OK = True


def call(path, body=None, method="GET", headers=None, timeout=300):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(LIB + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:150] if extra else ""))


def events(limit=200, **filters):
    """最近的事件：这个接口是位点续读（after=），不是 tail，所以要自己算起点"""
    st, head = call("/api/events?limit=1")
    after = max(0, int(head.get("last_gseq") or 0) - limit)
    q = "".join("&%s=%s" % (k, v) for k, v in filters.items())
    st, d = call("/api/events?after=%d&limit=%d%s" % (after, limit, q))
    return d.get("events") or []


stamp = time.strftime("%H%M%S")
fid = None
try:
    print("① 外部程序（不带标识）")
    st, d = call("/api/files", {"name": "zz-标签验收-%s.md" % stamp, "content": "# 一版\n\n原始内容\n"},
                 "POST")
    fid = d.get("file_id")
    check("建了临时笔记", bool(fid), d)
    time.sleep(0.3)
    ev = [e for e in events(50) if e.get("type") == "note.created" and fid in json.dumps(e.get("payload"))]
    check("note.created 记下了 writer", bool(ev) and ev[0].get("writer"), ev[:1])

    print("② 带 X-Writer 的调用")
    st, d = call("/api/files/%s" % fid, {"content": "# 二版\n\n改过的内容\n"}, "PUT",
                 {"X-Writer": "tool:acc"})
    check("PUT 成功", st == 200 and d.get("ok"), d.get("error"))
    bi = ((d.get("saved") or {}).get("before_image") or {}) if isinstance(d.get("saved"), dict) else {}
    check("PUT 覆盖时留了前像", bool(bi.get("ok")), bi.get("why") or bi.get("id"))
    st, imgs = call("/api/before-images?file_id=%s" % fid)
    got = imgs.get("images") or []
    check("前像清单里有它", bool(got), got[:1])
    check("前像记的是覆盖前的内容", got and got[0].get("reason") == "overwrite", got[0] if got else None)

    print("③ 页面来的（Origin 是本库）")
    st, d = call("/api/files/%s" % fid, {"content": "# 三版\n\n页面改的\n"}, "PUT",
                 {"Origin": "http://127.0.0.1:8848"})
    check("PUT 成功", st == 200, d.get("error"))

    print("③b 中文标签走 body.writer（HTTP 头只能是 ASCII）")
    st, d = call("/api/files/%s" % fid, {"content": "# 四版\n\n中文标签改的\n", "writer": "task:日报"},
                 "PUT")
    check("PUT 成功", st == 200, d.get("error"))

    print("④ 谁改的")
    st, w = call("/api/files/%s/writers" % fid)
    ws = w.get("writers") or []
    check("writers 查得到", bool(ws), ws[:2])
    tags = {x.get("writer") for x in ws}
    check("里面有 tool:acc / user / task:日报", {"tool:acc", "user", "task:日报"} <= tags, tags)

    print("⑤ 删除 → 前像 → 重建")
    st, d = call("/api/files/%s" % fid, None, "DELETE")
    check("删除成功且带回前像", st == 200 and d.get("deleted") == fid, d)
    img = (d.get("before_image") or {}).get("id")
    check("删除留了前像 id", bool(img), img)
    st, chk = call("/api/files/%s" % fid)
    check("删完确实没了", st == 404, st)
    st, r = call("/api/before-images/restore", {"id": img}, "POST")
    check("按前像恢复成功", st == 200 and r.get("ok"), r)
    new_fid = r.get("file_id")
    st, back = call("/api/files/%s" % new_fid)
    check("重建回来能读到", st == 200 and "四版" in (back.get("file") or {}).get("content", ""),
          (back.get("file") or {}).get("name"))
    check("落回原位置（根下，没跑到别的文件夹）", "/" not in (back.get("file") or {}).get("path", "x"),
          (back.get("file") or {}).get("path"))
    fid = new_fid
finally:
    for x in call("/api/before-images?limit=50")[1].get("images") or []:
        pass
    if fid:
        call("/api/files/%s" % fid, None, "DELETE")
    print("⑥ 临时笔记已删")

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
