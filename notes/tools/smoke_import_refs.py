"""入库自动转换自测：磁盘 `[[目标]]` → 库内 `@目标`（辞玖 2026-09-22 定）。

  python tools/smoke_import_refs.py

验五件事（都只碰临时目录）：
  ① 目标篇存在时，`[[目标]]` 入库即变 `@目标`，`[[目标#小节]]` → `@目标#小节token`
  ② 目标不存在 / 有歧义 → **原样不动**（宁可不转，也不写一个解析不回去的引用）
  ③ 代码围栏与行内代码里的 `[[...]]` 一律不动
  ④ convert_refs=False 时退回老行为（原样入库）
  ⑤ 返回里带 refs_converted，能看出这次转了几处
"""
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, "src")
from db import Database                       # noqa: E402
from importer import import_path              # noqa: E402

OK = True


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:150] if extra else ""))


def body(db, name):
    r = db.conn.execute("SELECT id FROM files WHERE name=?", (name,)).fetchone()
    return db.get_content(r["id"]).decode("utf-8")


src = tempfile.mkdtemp(prefix="ai-notes-refs-src-")
data = tempfile.mkdtemp(prefix="ai-notes-refs-db-")
target = os.path.join(src, "目标篇.md")
person = os.path.join(src, "引用方.md")
try:
    db = Database(data)
    with open(target, "wb") as f:
        f.write("# 目标篇\n\n## P12 决策前置\n\n正文\n".encode("utf-8"))
    import_path(db, target, under="vault/笔记", convert_refs=False)

    print("① 目标存在 → 入库即转")
    with open(person, "wb") as f:
        f.write(("# 引用方\n\n"
                 "见 [[目标篇]] 与 [[目标篇#P12 决策前置]]。\n"
                 "对不上的 [[查无此篇]] 与行内代码 `[[目标篇]]` 都别动。\n"
                 "```\n[[目标篇]]\n```\n").encode("utf-8"))
    r1 = import_path(db, person, under="vault/笔记")
    got = body(db, "引用方.md")
    check("imported=1", r1["imported"] == 1, r1.get("imported"))
    check("[[目标篇]] → @目标篇", "见 @目标篇 与" in got, got.splitlines()[2] if len(got.splitlines()) > 2 else got)
    check("[[目标篇#P12 决策前置]] → @目标篇#P12", "@目标篇#P12" in got, got[got.find("@目标篇"):][:40])
    check("目标对不上 → 原样留着", "[[查无此篇]]" in got)
    check("行内代码里的不动", "`[[目标篇]]`" in got)
    check("代码围栏里的不动", "```\n[[目标篇]]\n```" in got)
    check("refs_converted=2（只算真转的）", r1["refs_converted"] == 2, r1.get("refs_converted"))

    print("② 关掉开关 → 原样入库（老行为）")
    off = os.path.join(src, "不转的.md")
    with open(off, "wb") as f:
        f.write("# 不转的\n\n见 [[目标篇]]。\n".encode("utf-8"))
    r2 = import_path(db, off, under="vault/笔记", convert_refs=False)
    check("正文保持 [[目标篇]]", "[[目标篇]]" in body(db, "不转的.md"), r2.get("refs_converted"))
    check("refs_converted=0", r2["refs_converted"] == 0)

    print("③ 覆盖更新时也按同一规则（不会把库内 @ 变回 [[ ]]）")
    with open(person, "wb") as f:
        f.write("# 引用方\n\n见 [[目标篇]] 与 [[目标篇#P12 决策前置]]，外加 [[查无此篇]]。\n".encode("utf-8"))
    r3 = import_path(db, person, under="vault/笔记", overwrite=True)
    got3 = body(db, "引用方.md")
    check("updated=1", r3["updated"] == 1, r3.get("updated"))
    check("库里仍是 @目标篇", "@目标篇" in got3 and "[[目标篇]]" not in got3, got3.splitlines()[2][:50])

    db.close()
finally:
    shutil.rmtree(src, ignore_errors=True)
    shutil.rmtree(data, ignore_errors=True)
    print("④ 临时目录已清理")

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
