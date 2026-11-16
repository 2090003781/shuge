"""标签与检索范围的冒烟：带 logs 标签的文件默认不被检索，scope=all 才纳入。"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
ok_all = True
MARK = "ZZTAGMARKER"


def call(path, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra)) if extra else ""))


def search(term, scope=""):
    q = "/api/search?q=%s&limit=20%s" % (urllib.parse.quote(term), ("&scope=" + scope) if scope else "")
    return call(q)[1]


files_before = call("/api/health")[1]["stats"]["files"]

print("① 造一篇带标记词的临时笔记")
st, made = call("/api/files", "POST", {"name": "zz-冒烟-标签.md",
                                       "content": "# 标签冒烟\n\n%s 这个词只在这篇里。\n" % MARK})
fid = made["file_id"]
try:
    r = search(MARK)
    check("未打标时能被检索到", any(h["file_id"] == fid for h in r["hits"]), len(r["hits"]))

    print("② 打 logs 标签")
    st, r2 = call("/api/files/%s/tags" % fid, "POST", {"tags": ["logs"], "mode": "add"})
    check("打标成功", st == 200 and r2["tags"] == ["logs"], r2.get("tags"))
    _, f = call("/api/files/" + fid)
    check("读文件带出标签", f["file"].get("tags") == ["logs"], f["file"].get("tags"))

    print("③ 默认检索应排除它")
    r = search(MARK)
    check("默认搜不到", not any(h["file_id"] == fid for h in r["hits"]), len(r["hits"]))
    check("响应说明排除了什么", r.get("excluded_tags") == ["logs"], r.get("excluded_tags"))

    print("④ scope=all 时纳入")
    r = search(MARK, "all")
    mine = [h for h in r["hits"] if h["file_id"] == fid]
    check("全范围搜得到", bool(mine), "%d 条" % len(r["hits"]))
    check("命中带标签字段", bool(mine) and mine[0].get("tags") == "logs",
          mine[0].get("tags") if mine else None)

    print("⑤ 关键词通道同样生效")
    r2 = search(MARK, "") if False else call("/api/search?q=%s&mode=kw&limit=20" % urllib.parse.quote(MARK))[1]
    check("kw 模式默认排除", not any(h["file_id"] == fid for h in r2["hits"]))

    print("⑥ 标签统计")
    _, t = call("/api/tags")
    check("统计里有 logs", t["tags"].get("logs", 0) >= 1, t["tags"])

    print("⑦ 去掉标签后恢复")
    call("/api/files/%s/tags" % fid, "POST", {"tags": ["logs"], "mode": "remove"})
    r = search(MARK)
    check("默认又能搜到", any(h["file_id"] == fid for h in r["hits"]))
finally:
    call("/api/files/" + fid, "DELETE")
    h = call("/api/health")[1]["stats"]
    check("测试笔记已清理", h["files"] == files_before, "%s vs %s" % (h["files"], files_before))

print("\n========  结论：%s" % ("全部通过" if ok_all else "有失败项"))
sys.exit(0 if ok_all else 1)
