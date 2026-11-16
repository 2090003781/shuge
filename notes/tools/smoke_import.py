"""导入器的冒烟：编码归一（BOM / GBK）、--under 落点、内容去重、点文件跳过。"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, "src")
from db import Database            # noqa: E402
from importer import import_from_dir  # noqa: E402

ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra)) if extra else ""))


src = tempfile.mkdtemp(prefix="ai-notes-src-")
data = tempfile.mkdtemp(prefix="ai-notes-db-")
try:
    os.makedirs(os.path.join(src, "笔记"))
    open(os.path.join(src, "笔记", "utf8.md"), "wb").write("中文 UTF-8".encode("utf-8"))
    open(os.path.join(src, "笔记", "bom.md"), "wb").write(b"\xef\xbb\xbf" + "# 带 BOM\n正文".encode("utf-8"))
    open(os.path.join(src, "笔记", "gbk.md"), "wb").write("中文 GBK 测试".encode("gb18030"))
    open(os.path.join(src, ".cache.json"), "wb").write(b"{}")
    open(os.path.join(src, "同名.md"), "wb").write("原文".encode("utf-8"))
    os.makedirs(os.path.join(src, ".git"))
    open(os.path.join(src, ".git", "config"), "wb").write(b"x")

    db = Database(data)
    res = import_from_dir(db, src, under="vault", tag="imported")
    print("① 导入：%s" % {k: res[k] for k in ("imported", "skipped", "failed", "tagged", "reencoded", "under")})
    check("导入了 4 个文本文件（点文件/点目录被跳过）", res["imported"] == 4, res["imported"])
    check("其中 2 个走了转码（BOM + GBK）", res["reencoded"] == 2, res["reencoded"])

    print("② 落点与编码")
    rows = {r["name"]: r for r in db.conn.execute(
        "SELECT f.name, f.content, fo.name AS folder FROM files f JOIN folders fo ON fo.id=f.folder_id")}
    check("子目录文件落在 vault/笔记 下", rows["utf8.md"]["folder"] == "笔记", rows["utf8.md"]["folder"])
    check("源根下文件落在 vault 根", rows["同名.md"]["folder"] == "vault", rows["同名.md"]["folder"])
    check("utf8 原样", rows["utf8.md"]["content"].decode("utf-8") == "中文 UTF-8")
    bom_text = rows["bom.md"]["content"].decode("utf-8")
    check("BOM 已去掉", not bom_text.startswith("\ufeff"), repr(bom_text[:6]))
    check("BOM 文件正文完整", bom_text.startswith("# 带 BOM"), repr(bom_text[:12]))
    check("GBK 正确转成 UTF-8", rows["gbk.md"]["content"].decode("utf-8") == "中文 GBK 测试",
          repr(rows["gbk.md"]["content"].decode("utf-8")))

    print("③ 标签与树")
    check("打了标签", db.tag_stats().get("imported") == 4, db.tag_stats())
    tree = db.tree()
    check("顶层就是 vault", [c["name"] for c in tree["children"]] == ["vault"],
          [c["name"] for c in tree["children"]])

    print("④ 重复导入不重复入库")
    res2 = import_from_dir(db, src, under="vault")
    check("第二次全部跳过", res2["imported"] == 0 and res2["skipped"] == 4, res2)
    db.close()
finally:
    shutil.rmtree(src, ignore_errors=True)
    shutil.rmtree(data, ignore_errors=True)
    print("⑤ 临时目录已清理")

print("\n========  结论：%s" % ("全部通过" if ok_all else "有失败项"))
sys.exit(0 if ok_all else 1)
