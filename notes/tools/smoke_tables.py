"""表格线第 1 步自测：csv/tsv 建表 + 只读查询（白名单与参数绑定）+ 原文件留档 + 重建 + 删表。

  python tools/smoke_tables.py
"""
import io
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

LIB = os.environ.get("AINOTES_URL", "http://127.0.0.1:8848")
OK = True


def call(path, body=None, method="GET", timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(LIB + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:200]}


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:150] if extra else ""))


def files_count():
    return call("/api/health")[1].get("stats", {}).get("files")


before = files_count()
print("开工前篇数：", before)

tmp = tempfile.mkdtemp(prefix="zz-表格测试-")
csv_path = os.path.join(tmp, "客户名单.csv")
io.open(csv_path, "w", encoding="utf-8-sig", newline="").write(
    "姓名,城市,订单数,金额\n"
    "张三,杭州,3,1200.5\n"
    "李四,北京,1,88\n"
    "王五,杭州,7,9999.9\n"
    '赵六,"上海, 浦东",2,300\n')
tsv_path = os.path.join(tmp, "小表.tsv")
io.open(tsv_path, "w", encoding="utf-8", newline="").write("a\tb\n1\tx\n2\ty\n")

name = None
try:
    print("① 从 csv 建表")
    st, d = call("/api/tables", {"src": csv_path}, "POST")
    t = d.get("table") or {}
    name = t.get("name")
    check("建表成功", st == 200 and d.get("ok") is True and t.get("rows") == 4, d)
    check("列类型推出来了", [c["type"] for c in t.get("columns", [])][2:] == ["INTEGER", "REAL"],
          t.get("columns"))
    check("原文件同时留档进库（默认不进检索）",
          (d.get("file") or {}).get("imported") == 1 and (d.get("file") or {}).get("rag_off") is True,
          d.get("file"))
    check("篇数 +1", files_count() == before + 1, files_count())

    print("② 列表与只读查询")
    st, d = call("/api/tables")
    check("列出了这张表", any(x["name"] == name for x in d.get("tables", [])), [x["name"] for x in d.get("tables", [])])
    st, d = call("/api/table_rows?name=" + urllib.parse.quote(name) + "&limit=10")
    check("取到 4 行", d.get("ok") is True and d.get("total") == 4 and len(d.get("rows", [])) == 4, d.get("total"))
    check("带引号里的逗号没被切坏",
          any(r.get("城市") == "上海, 浦东" for r in d.get("rows", [])),
          [r.get("城市") for r in d.get("rows", [])])

    st, d = call("/api/table_rows?name=%s&q=%s" % (urllib.parse.quote(name), urllib.parse.quote("杭州")))
    check("q 子串过滤（参数绑定）", len(d.get("rows", [])) == 2, [r.get("姓名") for r in d.get("rows", [])])
    st, d = call("/api/table_rows?name=%s&sort=%s&desc=1&limit=1"
                 % (urllib.parse.quote(name), urllib.parse.quote("订单数")))
    check("排序 + desc + limit", d.get("rows") and d["rows"][0].get("订单数") == 7, d.get("rows"))

    print("③ 注入/越权都不该通")
    st, d = call("/api/table_rows?name=" + urllib.parse.quote(name + "; DROP TABLE files"))
    check("表名不在白名单 → 404", st == 404 and d.get("ok") is False, d)
    st, d = call("/api/table_rows?name=" + urllib.parse.quote(name)
                 + "&sort=" + urllib.parse.quote("name; DROP TABLE files"))
    check("排序列名不在白名单 → 被忽略（不报错）", d.get("ok") is True and d.get("sort") == "", d.get("sort"))

    print("④ 重建（replace）与 tsv")
    io.open(csv_path, "a", encoding="utf-8", newline="").write("孙七,广州,5,500\n")
    st, d = call("/api/tables", {"src": csv_path, "replace": True}, "POST")
    check("replace 重建后 5 行", (d.get("table") or {}).get("rows") == 5 and (d.get("table") or {}).get("replaced") is True,
          d.get("table"))
    st, d = call("/api/tables", {"src": tsv_path}, "POST")
    check("tsv 也认（按制表符）", (d.get("table") or {}).get("rows") == 2, d.get("table"))
    tsv_name = (d.get("table") or {}).get("name")

    print("⑤ 删表（原文件还在）")
    st, d = call("/api/table_drop", {"name": name}, "POST")
    check("删表成功", d.get("ok") is True and d.get("dropped") == name, d)
    st, d = call("/api/tables")
    check("列表里没了", not any(x["name"] == name for x in d.get("tables", [])))
    st, d = call("/api/table_drop", {"name": name}, "POST")
    check("再删同一个 → 404", st == 404, d)
finally:
    print("⑥ 清场")
    if name:
        call("/api/table_drop", {"name": name}, "POST")
    try:
        st, d = call("/api/tables")
        for t in d.get("tables", []):
            call("/api/table_drop", {"name": t["name"]}, "POST")
    except Exception:
        pass
    # 删库里留档的那两个文件
    st, lst = call("/api/rag/list?on=0&limit=3000")
    for f in lst.get("files", []):
        if f["name"] in ("客户名单.csv", "小表.tsv"):
            call("/api/files/" + f["id"], method="DELETE")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    time.sleep(0.5)
    after = files_count()
    check("篇数回到开工前", after == before, "before=%s after=%s" % (before, after))

print("=" * 60)
print("表格线第 1 步自测：", "全部通过" if OK else "有失败")
sys.exit(0 if OK else 1)
