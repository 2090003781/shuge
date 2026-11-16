"""表格线第 2 步自测：xlsx → 表（一个工作簿多个 sheet → 多张表）。

  python tools/smoke_tables_xlsx.py

xlsx 用**标准库当场造**（zipfile + XML），不依赖 Excel/openpyxl —— 与项目里 docx/odt 解析同一路子。
特意造两个坑：
  · 第 3 行故意跳过 B 列（A、C 有值）→ 必须按 r="A3"/"C3" 补位，不能整行左移；
  · sheet2 只有两行小表 → 两张表各自独立建。
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
import zipfile

LIB = os.environ.get("AINOTES_URL", "http://127.0.0.1:8848")
M = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"
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


def make_xlsx(path):
    """手写一个最小 xlsx：两个 sheet，含共享字符串/数字/空列。"""
    shared = ["姓名", "城市", "订单数", "金额", "张三", "杭州", "李四", "甲", "乙"]
    sst = ('<?xml version="1.0" encoding="UTF-8"?><sst xmlns="%s" count="%d" uniqueCount="%d">%s</sst>'
           % (M, len(shared), len(shared),
              "".join('<si><t>%s</t></si>' % s for s in shared)))
    sheet1 = ('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="%s"><sheetData>'
              '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
              '<c r="C1" t="s"><v>2</v></c><c r="D1" t="s"><v>3</v></c></row>'
              '<row r="2"><c r="A2" t="s"><v>4</v></c><c r="B2" t="s"><v>5</v></c>'
              '<c r="C2"><v>3</v></c><c r="D2"><v>1200.5</v></c></row>'
              # 故意跳过 B 列：A/C 有值 → 必须补位（B 为空），否则整行左移
              '<row r="3"><c r="A3" t="s"><v>6</v></c><c r="C3"><v>1</v></c></row>'
              '</sheetData></worksheet>') % M
    sheet2 = ('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="%s"><sheetData>'
              '<row r="1"><c r="A1" t="s"><v>7</v></c><c r="B1" t="s"><v>8</v></c></row>'
              '<row r="2"><c r="A2"><v>1</v></c><c r="B2"><v>2</v></c></row>'
              '</sheetData></worksheet>') % M
    wb = ('<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="%s" xmlns:r="%s"><sheets>'
          '<sheet name="客户" sheetId="1" r:id="rId1"/>'
          '<sheet name="小表" sheetId="2" r:id="rId2"/></sheets></workbook>') % (M, R)
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="%s">'
            '<Relationship Id="rId1" Type="%s/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="%s/worksheet" Target="worksheets/sheet2.xml"/>'
            '</Relationships>') % (P, R, R)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/sharedStrings.xml", sst)
        z.writestr("xl/worksheets/sheet1.xml", sheet1)
        z.writestr("xl/worksheets/sheet2.xml", sheet2)


before = call("/api/health")[1]["stats"]["files"]
print("开工前篇数：", before)
tmp = tempfile.mkdtemp(prefix="zz-xlsx测试-")
xlsx = os.path.join(tmp, "销售台账.xlsx")
make_xlsx(xlsx)
names = []
try:
    print("① 一个工作簿 → 多张表")
    st, d = call("/api/tables", {"src": xlsx}, "POST")
    ts = d.get("tables") or []
    names = [t["name"] for t in ts]
    check("建出 2 张表", st == 200 and d.get("ok") is True and len(ts) == 2, d.get("count"))
    check("表名带 sheet 名", any("客户" in n for n in names) and any("小表" in n for n in names), names)
    check("原文件同时留档进库（默认不进检索）",
          (d.get("file") or {}).get("imported") == 1 and (d.get("file") or {}).get("rag_off") is True,
          d.get("file"))
    cust = next(t for t in ts if "客户" in t["name"])
    check("列名与类型对", [c["col"] for c in cust["columns"]] == ["姓名", "城市", "订单数", "金额"]
          and [c["type"] for c in cust["columns"]] == ["TEXT", "TEXT", "INTEGER", "REAL"],
          cust["columns"])
    check("行数对（表头不算）", cust["rows"] == 2, cust["rows"])

    print("② 空列补位（第 3 行只有 A、C）")
    st, d = call("/api/table_rows?name=" + urllib.parse.quote(cust["name"]) + "&sort=" +
                 urllib.parse.quote("姓名"))
    rows = d.get("rows", [])
    check("两行都在", len(rows) == 2, rows)
    li = next((r for r in rows if r.get("姓名") == "李四"), None)
    check("李四那行：城市为空、订单数=1（没左移）", li and li.get("城市") == "" and li.get("订单数") == 1, li)

    print("③ 第二张表独立")
    st, d = call("/api/tables")
    check("列表里有 2 张表", len([x for x in d.get("tables", []) if x["name"] in names]) == 2,
          [x["name"] for x in d.get("tables", [])])
finally:
    print("④ 清场")
    for n in names:
        call("/api/table_drop", {"name": n}, "POST")
    st, lst = call("/api/rag/list?on=0&limit=3000")
    for f in lst.get("files", []):
        if f["name"] == "销售台账.xlsx":
            call("/api/files/" + f["id"], method="DELETE")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    time.sleep(0.5)
    after = call("/api/health")[1]["stats"]["files"]
    check("篇数回到开工前", after == before, "before=%s after=%s" % (before, after))
    st, d = call("/api/tables")
    check("表也清干净了", not [x for x in d.get("tables", []) if x["name"] in names])

print("=" * 60)
print("xlsx 建表自测：", "全部通过" if OK else "有失败")
sys.exit(0 if OK else 1)
