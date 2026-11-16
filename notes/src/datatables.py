"""表格层（2026-09-21 辞玖定的分工：文档走文件树+RAG，表格/表单走 SQL）。

第一版只做 csv/tsv：
    · 原文件**留档**（照旧当"文件"索引进库，默认不进检索）；
    · 解析成**真正的 SQL 表**（行列结构 = 这类数据的真相），可查、可筛、可排序；
    · 表是**派生视图**：随时可以从原文件重建（replace=true 就先删后建）。

安全（这是表格线最容易出事的地方）：
    · **只读查询**：只接受 SELECT；表名/列名必须来自登记表 data_tables 的白名单；
    · 值一律走 **参数绑定**（绝不拼字符串）——sqlite 的 `?` 占位；
    · 不暴露任何"执行任意 SQL"的接口。

登记表：
    data_tables(name PK, title, source_path, columns(JSON), row_count, created_at, updated_at)
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3

from db import local_iso  # noqa: E402

TABLE_PREFIX = "t_"
MAX_ROWS = 200000


def _slug(name: str) -> str:
    """标识符清洗：保留中文/字母/数字/下划线（标识符全程加引号，中文是安全的）。

    2026-09-21 踩到：原来把非 ASCII 一律替换成 _ 再兜底成 "table"，
    结果「姓名/城市/订单数/金额」四个列名全塌成 table / table_ / table__ …。
    """
    s = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff]+", "_", name or "").strip("_")
    return s[:40]


def ensure_registry(db) -> None:
    db.conn.execute("""CREATE TABLE IF NOT EXISTS data_tables(
        name TEXT PRIMARY KEY, title TEXT, source_path TEXT, columns TEXT,
        row_count INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT)""")
    db.conn.commit()


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _infer(values: list) -> str:
    vals = [v for v in values if v not in ("", None)]
    if not vals:
        return "TEXT"
    try:
        for v in vals:
            int(str(v).replace(",", ""))
        return "INTEGER"
    except Exception:
        pass
    try:
        for v in vals:
            float(str(v).replace(",", ""))
        return "REAL"
    except Exception:
        return "TEXT"


def read_csv(path: str, delimiter: str | None = None, limit: int = MAX_ROWS):
    """读 csv/tsv → (表头, 行列表)。中文 Excel 导出的 csv 常见 GBK，所以按几种编码依次试。"""
    with open(path, "rb") as f:
        raw = f.read()
    text = _decode(raw)
    if delimiter is None:
        head = text.split("\n", 1)[0]
        delimiter = "\t" if head.count("\t") > head.count(",") else ","
    rdr = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = []
    header = None
    for i, row in enumerate(rdr):
        if i == 0:
            header = [(c or "").strip() or ("col%d" % (j + 1)) for j, c in enumerate(row)]
            continue
        if not any((c or "").strip() for c in row):
            continue
        rows.append(row)
        if len(rows) >= limit:
            break
    return header or [], rows


def table_name_for(db, title: str) -> str:
    base = TABLE_PREFIX + (_slug(title) or "table")
    name, i = base, 1
    names = {r[0] for r in db.conn.execute("SELECT name FROM data_tables")} if ensure_ok(db) else set()
    while name in names:
        i += 1
        name = "%s_%d" % (base, i)
    return name


def ensure_ok(db) -> bool:
    try:
        db.conn.execute("SELECT 1 FROM data_tables LIMIT 1")
        return True
    except sqlite3.Error:
        return False


def create_from_csv(db, path: str, title: str = "", replace: bool = False,
                    delimiter: str | None = None) -> dict:
    """把一个 csv/tsv 建/重建为 SQL 表。返回 {name, columns, rows, replaced}"""
    ensure_registry(db)
    if not os.path.isfile(path):
        return {"error": "文件不存在: %s" % path}
    title = title or os.path.splitext(os.path.basename(path))[0]
    header, rows = read_csv(path, delimiter=delimiter)
    if not header:
        return {"error": "这个文件读不出表头（空文件或不是 csv/tsv）"}
    cols = []
    for j, h in enumerate(header):
        col = _slug(h) or ("col%d" % (j + 1))
        while col in [c[0] for c in cols]:
            col += "_"
        cols.append((col, _infer([r[j] if j < len(r) else "" for r in rows])))
    name = TABLE_PREFIX + (_slug(title) or "table")
    old = db.conn.execute("SELECT name FROM data_tables WHERE name=?", (name,)).fetchone()
    if old and not replace:
        name = table_name_for(db, title)          # 不覆盖：换个名字建
    elif old and replace:
        db.conn.execute('DROP TABLE IF EXISTS "%s"' % name)
        db.conn.execute("DELETE FROM data_tables WHERE name=?", (name,))
    db.conn.execute('CREATE TABLE "%s" (%s)' % (
        name, ", ".join('"%s" %s' % (c, t) for c, t in cols)))
    ph = ", ".join("?" * len(cols))
    payload = []
    for r in rows:
        payload.append([ (r[j] if j < len(r) else "") for j in range(len(cols)) ])
    if payload:
        db.conn.executemany('INSERT INTO "%s" VALUES (%s)' % (name, ph), payload)
    now = local_iso()
    db.conn.execute("INSERT OR REPLACE INTO data_tables(name,title,source_path,columns,row_count,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (name, title, path, json.dumps([{"col": c, "type": t} for c, t in cols],
                                                   ensure_ascii=False),
                     len(payload), now, now))
    db.conn.commit()
    return {"name": name, "title": title, "columns": [{"col": c, "type": t} for c, t in cols],
            "rows": len(payload), "replaced": bool(old and replace), "source": path}


def list_tables(db) -> list:
    ensure_registry(db)
    out = []
    for r in db.conn.execute("SELECT name,title,source_path,columns,row_count,created_at,updated_at "
                             "FROM data_tables ORDER BY updated_at DESC"):
        out.append({"name": r["name"], "title": r["title"], "source_path": r["source_path"],
                    "columns": json.loads(r["columns"] or "[]"), "rows": r["row_count"],
                    "created_at": r["created_at"], "updated_at": r["updated_at"]})
    return out


def _columns_of(db, name: str):
    row = db.conn.execute("SELECT columns FROM data_tables WHERE name=?", (name,)).fetchone()
    if row is None:
        return None
    return [c["col"] for c in json.loads(row["columns"] or "[]")]


def rows(db, name: str, limit: int = 50, offset: int = 0, sort: str = "", desc: bool = False,
         q: str = "") -> dict:
    """只读查询：表名与列名走白名单，值走参数绑定。"""
    cols = _columns_of(db, name)
    if cols is None:
        return {"error": "没有这张表: %s" % name}
    limit = max(1, min(int(limit or 50), 1000))
    offset = max(0, int(offset or 0))
    sql = 'SELECT * FROM "%s"' % name
    args: list = []
    if q:
        # 子串过滤：所有列 OR 起来，值绑参数（不拼字符串）
        sql += " WHERE (" + " OR ".join('CAST("%s" AS TEXT) LIKE ?' % c for c in cols) + ")"
        args += ["%" + q + "%"] * len(cols)
    if sort in cols:
        sql += ' ORDER BY "%s" %s' % (sort, "DESC" if desc else "ASC")
    sql += " LIMIT ? OFFSET ?"
    args += [limit, offset]
    cur = db.conn.execute(sql, args)
    out = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
    total = db.conn.execute('SELECT COUNT(*) FROM "%s"' % name).fetchone()[0]
    return {"name": name, "columns": cols, "rows": out, "count": len(out), "total": total,
            "limit": limit, "offset": offset, "sort": sort if sort in cols else "",
            "q": q}


def drop_table(db, name: str) -> dict:
    ensure_registry(db)
    if not db.conn.execute("SELECT 1 FROM data_tables WHERE name=?", (name,)).fetchone():
        return {"error": "没有这张表: %s" % name}
    db.conn.execute('DROP TABLE IF EXISTS "%s"' % name)
    db.conn.execute("DELETE FROM data_tables WHERE name=?", (name,))
    db.conn.commit()
    return {"dropped": name}


"""一次性（表格线第 2 步）：xlsx → 表（零依赖：zip + XML，与项目里 docx/odt 的解析同一路子）。

    · 读 xl/workbook.xml 拿 sheet 名单与顺序、xl/_rels/workbook.xml.rels 拿 r:id → 文件
    · 读 xl/sharedStrings.xml 拿共享字符串
    · 每个 sheet 的 xl/worksheets/*.xml 里按 <c r="B3" t="s"><v>…</v> 取值；列字母换算成序号，
      空单元格按 r 属性补位（不然后面整列错位）
    · 一个 sheet = 一张表（表名 t_<文件名>_<sheet名>）；首行当表头
    · 只认值：t="s"（共享串）/t="inlineStr"/t="str"/t="b"（TRUE/FALSE）/无 t（数字）
"""
import io
import os
import re
import zipfile
import xml.etree.ElementTree as ET

NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
NS_PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"

CELL_RE = re.compile(r"^([A-Z]+)(\d+)$")


def _col_index(ref: str) -> int:
    m = CELL_RE.match(ref or "")
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _sheet_list(z: zipfile.ZipFile):
    """[(sheet 名, zip 内路径)]，保持工作簿里的顺序"""
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = {}
    try:
        rx = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        for r in rx:
            rels[r.get("Id")] = r.get("Target")
    except KeyError:
        pass
    out = []
    for sh in wb.iter(NS_MAIN + "sheet"):
        name = sh.get("name") or "sheet"
        rid = sh.get(NS_REL + "id") or ""
        target = rels.get(rid, "")
        if not target:
            continue
        path = target.lstrip("/")
        if not path.startswith("xl/"):
            path = "xl/" + path
        out.append((name, path))
    if not out:                                  # 兜底：直接找 worksheets
        out = [("sheet%d" % (i + 1), n) for i, n in
               enumerate(sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet")))]
    return out


def _shared_strings(z: zipfile.ZipFile) -> list:
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    out = []
    for si in root.iter(NS_MAIN + "si"):
        out.append("".join(t.text or "" for t in si.iter(NS_MAIN + "t")))
    return out


def _cell_value(c, shared: list) -> str:
    t = c.get("t") or ""
    if t == "inlineStr":
        return "".join(x.text or "" for x in c.iter(NS_MAIN + "t"))
    v = c.find(NS_MAIN + "v")
    raw = (v.text if v is not None else "") or ""
    if t == "s":
        try:
            return shared[int(raw)]
        except Exception:
            return raw
    if t == "b":
        return "TRUE" if raw == "1" else "FALSE"
    return raw                                   # 数字 / t="str" 公式结果


def read_xlsx(path: str) -> list:
    """读一个 xlsx → [{'sheet': 名, 'header': [...], 'rows': [[...]]}]（空 sheet 跳过）"""
    with zipfile.ZipFile(path) as z:
        shared = _shared_strings(z)
        sheets = []
        for name, spath in _sheet_list(z):
            try:
                root = ET.fromstring(z.read(spath))
            except KeyError:
                continue
            grid = []
            for row in root.iter(NS_MAIN + "row"):
                cells = {}
                for c in row.iter(NS_MAIN + "c"):
                    ref = c.get("r") or ""
                    idx = _col_index(ref) if ref else (max(cells) + 1 if cells else 0)
                    val = _cell_value(c, shared)
                    if val != "":
                        cells[idx] = val
                if not cells:
                    continue
                width = max(cells) + 1
                grid.append([cells.get(i, "") for i in range(width)])
            if not grid:
                continue
            width = max(len(r) for r in grid)
            grid = [r + [""] * (width - len(r)) for r in grid]
            header = [h.strip() or ("col%d" % (i + 1)) for i, h in enumerate(grid[0])]
            body = [r for r in grid[1:] if any((x or "").strip() for x in r)]
            if not body:                             # 只有表头没数据的 sheet 也建表（空表）
                body = []
            sheets.append({"sheet": name, "header": header, "rows": body})
        return sheets


def table_name_of(db, base: str) -> str:
    return TABLE_PREFIX + (_slug(base) or "table")


def create_from_xlsx(db, path: str, title: str = "", replace: bool = False,
                     only_sheet: str = "") -> dict:
    """把一个 xlsx 的每个 sheet 建成一张表。返回 {tables:[{name,sheet,columns,rows}], ...}"""
    ensure_registry(db)
    if not os.path.isfile(path):
        return {"error": "文件不存在: %s" % path}
    title = title or os.path.splitext(os.path.basename(path))[0]
    try:
        sheets = read_xlsx(path)
    except Exception as e:
        return {"error": "读 xlsx 失败：%s: %s" % (type(e).__name__, e)}
    if not sheets:
        return {"error": "这个 xlsx 里没有可用的表（空工作簿？）"}
    made = []
    for s in sheets:
        if only_sheet and s["sheet"] != only_sheet:
            continue
        base = "%s_%s" % (title, s["sheet"]) if len(sheets) > 1 else title
        name = table_name_of(db, base)
        prev = db.conn.execute("SELECT name FROM data_tables WHERE name=?", (name,)).fetchone()
        if prev and replace:
            db.conn.execute('DROP TABLE IF EXISTS "%s"' % name)
            db.conn.execute("DELETE FROM data_tables WHERE name=?", (name,))
        elif prev:
            name = table_name_for(db, base)
        cols = []
        for j, h in enumerate(s["header"]):
            col = _slug(h) or ("col%d" % (j + 1))
            while col in [c[0] for c in cols]:
                col += "_"
            cols.append((col, _infer([r[j] if j < len(r) else "" for r in s["rows"]])))
        db.conn.execute('CREATE TABLE "%s" (%s)' % (
            name, ", ".join('"%s" %s' % (c, t) for c, t in cols)))
        payload = [[(r[j] if j < len(r) else "") for j in range(len(cols))] for r in s["rows"]]
        if payload:
            db.conn.executemany('INSERT INTO "%s" VALUES (%s)' % (name, ", ".join("?" * len(cols))),
                                payload)
        now = local_iso()
        db.conn.execute("INSERT OR REPLACE INTO data_tables(name,title,source_path,columns,row_count,"
                        "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (name, base, path, json.dumps([{"col": c, "type": t} for c, t in cols],
                                                      ensure_ascii=False), len(payload), now, now))
        made.append({"name": name, "sheet": s["sheet"],
                     "columns": [{"col": c, "type": t} for c, t in cols], "rows": len(payload)})
    db.conn.commit()
    return {"tables": made, "count": len(made), "source": path, "sheets": len(sheets)}
