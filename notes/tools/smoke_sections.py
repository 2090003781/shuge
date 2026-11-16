"""按小节改（`POST /api/files/<id>/sections`）与单篇重建（`.../reindex`）的验收。

    python tools/smoke_sections.py

按小节改是"写正文的第三种形态"（整篇 set / 尾部 append / **按节** section）：
只动那一节，**其余一个字不碰** —— 这条是它存在的理由，所以重点验"别处没被碰"。
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
OK, FAILED = True, []

BODY = """# 标题

前言一段。

## 第一节
第一节的正文。
第二行。

## 第二节
第二节的正文。

### 第二点一
更深的正文。

## 第三节
第三节的正文。
"""


def call(path, method="GET", body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    if not cond:
        FAILED.append(name)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:170] if extra else ""))


def text_of(fid):
    return (call("/api/files/" + fid)[1].get("file") or {}).get("content") or ""


def sec(text, name):
    """抠出某一节（从它的标题到下一个同级/更高级标题）—— 用来验「别处没被碰」"""
    lines = text.split("\n")
    out, on, lvl = [], False, 0
    for l in lines:
        if l.startswith("#"):
            cur = len(l) - len(l.lstrip("#"))
            if on and cur <= lvl:
                break
            if not on and l.lstrip("#").strip() == name:
                on, lvl = True, cur
        if on:
            out.append(l)
    return "\n".join(out).strip()


made = []
try:
    st, d = call("/api/files", "POST", {"name": "zz-按节-测试", "format": "md",
                                        "content": BODY})
    fid = d["file_id"]
    made.append(fid)
    check("建好一篇四小节", st == 201 and fid, d.get("name"))
    lib = (call("/api/mirror")[1] or {}).get("library_dir") or ""
    disk = os.path.join(lib, d.get("name") or "")

    print("① replace：换一节正文，**别处一个字不碰**")
    st, r = call("/api/files/%s/sections" % fid, "POST",
                 {"mode": "replace", "name": "第二节", "text": "第二节换过了。\n新的一行。"})
    t = text_of(fid)
    check("回执 200 且说了做了什么", st == 200 and r.get("changed") and "replace" in (r.get("what") or ""),
          r.get("what"))
    check("那一节换成新正文", "第二节换过了。" in sec(t, "第二节") and "第二节的正文" not in sec(t, "第二节"),
          sec(t, "第二节").replace("\n", "⏎")[:80])
    check("第一节没被碰", sec(t, "第一节") == "## 第一节\n第一节的正文。\n第二行。")
    check("第三节没被碰", sec(t, "第三节") == "## 第三节\n第三节的正文。")
    check("更深的子节还在（第二点一）", "### 第二点一" in t and "更深的正文。" in t)
    check("盘上那份跟着变了", os.path.isfile(disk) and open(disk, encoding="utf-8").read() == t)
    check("前像留了（高风险可回滚）", bool(((r.get("saved") or {}).get("before_image") or {}).get("id"))
          or bool(call("/api/before-images?file_id=" + fid)[1].get("images")), None)

    print("② append：接在**那一节末尾**（不是文末）")
    st, r = call("/api/files/%s/sections" % fid, "POST",
                 {"mode": "append", "name": "第一节", "text": "接着第一节说一句。"})
    t = text_of(fid)
    i_app, i_sec2 = t.find("接着第一节说一句。"), t.find("## 第二节")
    check("追加的话落在第二节标题**之前**", 0 < i_app < i_sec2, (i_app, i_sec2))
    _s2 = sec(t, "第二节")
    check("第二节仍然没被碰（还是①改后的那份）",
          "第二节换过了。" in _s2 and "第二节的正文" not in _s2, _s2.replace("\n", "⏎")[:70])

    print("③ insert：加一节（指哪插哪 / 不指就文末）")
    st, r = call("/api/files/%s/sections" % fid, "POST",
                 {"mode": "insert", "name": "插进来的", "text": "新节正文。", "after": "第一节"})
    t = text_of(fid)
    i_new, i_sec2 = t.find("## 插进来的"), t.find("## 第二节")
    check("插在第一节之后、第二节之前", 0 < i_new < i_sec2, (i_new, i_sec2))
    check("层级跟着 after（二级）", "## 插进来的" in t, t[i_new:i_new + 20])
    st, r = call("/api/files/%s/sections" % fid, "POST",
                 {"mode": "insert", "name": "文末的", "text": "收尾。"})
    t2 = text_of(fid)
    check("不指 after 就放文末", t2.rstrip().endswith("收尾。"), t2[-40:].replace("\n", "⏎"))

    print("④ 改名（new_name）+ 小节引用跟着跟随")
    st, r = call("/api/files/%s/sections" % fid, "POST",
                 {"mode": "replace", "name": "第三节", "new_name": "第三节课", "text": "第三节的正文。"})
    t = text_of(fid)
    check("标题改了", "## 第三节课" in t and "## 第三节\n" not in t, [x for x in t.split("\n") if x.startswith("## ")])
    check("库里的小节表也认新名（按节引用能指到）",
          "第三节课" in ((call("/api/files/%s/sections" % fid, "POST",
                              {"mode": "delete", "name": "不存在的一节"})[1] or {}).get("sections") or []),
          None)

    print("⑤ delete：删一节，其余不动")
    st, r = call("/api/files/%s/sections" % fid, "POST", {"mode": "delete", "name": "文末的"})
    t = text_of(fid)
    check("那节没了", "文末的" not in t and "收尾。" not in t, r.get("what"))
    check("其它节还在", "## 第一节" in t and "## 插进来的" in t and "## 第三节课" in t)

    print("⑥ 该拒的拒")
    st, r = call("/api/files/%s/sections" % fid, "POST", {"mode": "replace", "name": "没有这一节", "text": "x"})
    check("找不到那一节 → 400 且列出有哪些节",
          st == 400 and "第一节" in json.dumps(r.get("sections") or [], ensure_ascii=False), r.get("error"))
    st, r = call("/api/files/%s/sections" % fid, "POST", {"mode": "乱写", "name": "第一节"})
    check("mode 不认识 → 400", st == 400, r.get("error"))
    st, r = call("/api/files/no-such-id/sections", "POST", {"mode": "replace", "name": "x"})
    check("不存在的篇 → 404", st == 404, r.get("error"))

    print("⑦ 单篇重建派生层")
    st, r = call("/api/files/%s/reindex" % fid, "POST", {"with_vectors": True})
    check("回 200 且报了块数", st == 200 and isinstance((r.get("reindex") or {}).get("chunks"), int),
          (r.get("reindex") or {}).get("chunks"))
    check("没改正文（内容一模一样）", text_of(fid) == t, None)
    st, r = call("/api/files/%s/reindex" % fid, "POST", {"force_parse": True})
    check("force_parse 也认（markdown 无解析层，不报错）", st == 200, r.get("reindex"))
    st, r = call("/api/files/no-such-id/reindex", "POST", {})
    check("不存在的篇 → 404", st == 404, r.get("error"))
finally:
    print("⑧ 收摊")
    if made:
        st, _ = call("/api/files/" + made[0], "DELETE")
        print("   删掉测试篇：", st)

print("=" * 70)
print("通过" if OK else "失败：%s" % "、".join(FAILED))
sys.exit(0 if OK else 1)
