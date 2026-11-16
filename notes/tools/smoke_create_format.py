"""新建文件的"格式"（POST /api/files 的 format）端到端冒烟。

辞玖 2026-10-03：**新建文件操作应该规定好确定格式这一项** ——
  1. 给了 format → 以它为准（名字里写没写后缀都行，两边一致就行）
  2. 名字的扩展名与 format 打架 → 400，不许猜
  3. 没给 format → 按名字的扩展名；扩展名不认识 → 400 并告诉它用 format
  4. 名字没有扩展名 → **默认 md**（原来会产出"没有格式"的 asset，那篇《手搓小夜模型》就是）
  5. 文本族（md/txt/jsonl…）写进去就是字节、kind 是文本、库内可编辑；文档族（docx…）现场生成真文档
自建自清：只造 zz- 开头的临时物，测完删干净。
"""
import io
import json
import os
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
ok_all = True


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
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:170] if extra else ""))


made = []
try:
    print("① 名字没有扩展名 → 默认 .md（原来会变成「没有格式」的 asset）")
    st, d = call("/api/files", "POST", {"name": "zz-格式-无后缀",
                                        "content": "# 无后缀\n\n我是 Markdown。\n"})
    made.append(d.get("file_id"))
    check("建成功且回执说清了名字与格式", st == 201 and d.get("name") == "zz-格式-无后缀.md"
          and d.get("format") == "md", {k: d.get(k) for k in ("name", "format", "generated")})
    f = call("/api/files/" + d["file_id"])[1]["file"]
    check("kind=markdown（不是 asset）", f["kind"] == "markdown", f["kind"])
    check("capabilities 说它能看能改能检索",
          f["caps"] == {"open": True, "edit": True, "search": True, "store_only": False}, f["caps"])

    print("② format 说了算：名字写不写后缀都行，别名也认")
    for fmt, want_name, want_kind in [("md", "zz-格式-别名.md", "markdown"),
                                      ("markdown", "zz-格式-别名2.md", "markdown"),
                                      ("txt", "zz-格式-纯文本", "text"),
                                      ("jsonl", "zz-格式-行json", "code"),
                                      (".csv", "zz-格式-逗号", "code")]:
        st, d = call("/api/files", "POST", {"name": want_name, "format": fmt,
                                            "content": "zz\n"})
        made.append(d.get("file_id"))
        nm = d.get("name") or ""
        ex = nm.rsplit(".", 1)[-1] if "." in nm else ""
        want_ext = {"markdown": "md", ".csv": "csv"}.get(fmt, fmt)
        check("format=%s → 名字 .%s" % (fmt, want_ext), st == 201 and ex == want_ext,
              {k: d.get(k) for k in ("name", "format")})
        if d.get("file_id"):
            f = call("/api/files/" + d["file_id"])[1]["file"]
            check("  kind=%s、可编辑" % want_kind, f["kind"] == want_kind and f["caps"]["edit"],
                  "%s %s" % (f["kind"], f["caps"]))

    print("③ 名字扩展名与 format 打架 → 400，不猜")
    st, d = call("/api/files", "POST", {"name": "zz-格式-打架.txt", "format": "docx",
                                        "content": "x"})
    check("400 且说清两边是什么", st == 400 and "txt" in d.get("error", "")
          and "docx" in d.get("error", ""), d.get("error"))
    st, d = call("/api/files", "POST", {"name": "zz-格式-打架.md", "format": "markdown",
                                        "content": "# 别名不算打架\n"})
    made.append(d.get("file_id"))
    check("别名（markdown↔md）不算打架", st == 201, d.get("error"))

    print("④ format 不认识 / 扩展名不认识 → 400 且给出可选项")
    st, d = call("/api/files", "POST", {"name": "zz-格式-瞎写", "format": "zzz", "content": "x"})
    check("format=zzz → 400", st == 400, d.get("error"))
    check("回执里列出能新建的格式", isinstance(d.get("formats"), list)
          and "md" in d["formats"] and "docx" in d["formats"], (d.get("formats") or [])[:8])
    st, d = call("/api/files", "POST", {"name": "zz-格式-怪后缀.zzz", "content": "x"})
    check("名字 .zzz 且没给 format → 400", st == 400, d.get("error"))
    st, d = call("/api/files", "POST", {"name": "zz-格式-怪后缀.zzz", "format": "txt",
                                        "content": "x"})
    check(".zzz 名字 + format=txt → 还是 400（两边必须一致）", st == 400, d.get("error"))

    print("⑤ 文档族：现场生成真文档，落盘就是那个格式")
    st, d = call("/api/files", "POST", {"name": "zz-格式-文档", "format": "docx",
                                        "content": "# 现场生成\n\n真 docx。\n"})
    made.append(d.get("file_id"))
    check("建成功且是生成的", st == 201 and d.get("generated")
          and d["generated"].get("to") == "docx", d.get("generated") or d)
    if d.get("file_id"):
        f = call("/api/files/" + d["file_id"])[1]["file"]
        check("kind=document、能看不能改", f["kind"] == "document"
              and f["caps"]["open"] and not f["caps"]["edit"], "%s %s" % (f["kind"], f["caps"]))
        # 原字节直出（不是 JSON，别拿 json.loads 解）
        body = urllib.request.urlopen(BASE + "/api/files/%s/raw" % d["file_id"],
                                      timeout=30).read()
        check("  PK\\x03\\x04 开头", body[:4] == b"PK\x03\x04", body[:8])

    print("⑥ content_b64（成品字节）与 format 二选一")
    import base64
    st, d = call("/api/files", "POST", {"name": "zz-格式-字节.png", "format": "png",
                                        "content_b64": base64.b64encode(b"\x89PNG\r\n\x1a\nx").decode()})
    check("同时给 content_b64 和 format → 400", st == 400, d.get("error"))
    st, d = call("/api/files", "POST", {"name": "zz-格式-字节.png",
                                        "content_b64": base64.b64encode(b"\x89PNG\r\n\x1a\nx").decode()})
    made.append(d.get("file_id"))
    check("只给 content_b64 → 照旧能建", st == 201, d.get("error"))

    print("⑦ 老写法不破：name 带 .md/.docx、不给 format，行为跟以前一样")
    st, d = call("/api/files", "POST", {"name": "zz-格式-老写法.md", "content": "# 老写法\n"})
    made.append(d.get("file_id"))
    check("name 带 .md → 建成功、format 回执是 md", st == 201 and d.get("format") == "md", d)
    st, d = call("/api/files", "POST", {"name": "zz-格式-老写法.docx", "content": "# 老写法\n"})
    made.append(d.get("file_id"))
    check("name 带 .docx → 还是现场生成", st == 201 and (d.get("generated") or {}).get("to") == "docx",
          d.get("generated"))
    print("⑨ 库内新建 → 磁盘上真有这个文件（库自己落盘，不靠人手动另存）")
    st, d = call("/api/files", "POST", {"name": "zz-格式-落盘", "format": "txt",
                                        "content": "落盘检查\n"})
    made.append(d.get("file_id"))
    lib = (call("/api/mirror")[1] or {}).get("library_dir") or ""
    # 注意：接口里的 `path` 是**引用路径**（去了 vault/ 与扩展名，给 @引用 用），不是盘上路径。
    # 建在根目录 → 根目录就是库根；用回执里的真名拼。
    disk = os.path.join(lib, d.get("name") or "")
    check("磁盘上有 %s" % disk, os.path.isfile(disk), disk)
    if os.path.isfile(disk):
        check("  磁盘内容与库里一致", io.open(disk, encoding="utf-8").read() == "落盘检查\n")

    print("⑩ 判 kind 的判据本身（本地直调 db.kind_for；覆盖「导入没有扩展名的文件」那条路）")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "src"))
    import db as _db
    K = _db.Database.__new__(_db.Database)        # 不落盘的壳：kind_for 用不到连接
    cases = [
        ("手搓小夜模型-完整过程记录", "", "# 手搓小夜模型\n\n正文".encode("utf-8"), "markdown"),
        ("没后缀纯文本", "", "中文，逗号，没有控制字符".encode("utf-8"), "text"),
        ("空文件", "", b"", "asset"),
        ("假文本.zip", "", b"PK\x03\x04\x14\x00\x02\x08", "asset"),
        ("zz.jsonl", "jsonl", b'{"a": 1}\n', "code"),
        ("zz.不认识", "zzy", "就是文本\n".encode("utf-8"), "text"),
        ("zz.png", "png", b"\x89PNG\r\n\x1a\n", "image"),
        ("zz.docx", "docx", b"PK\x03\x04\x14\x00\x02\x08", "document"),
        ("zz.md", "md", b"# t\n", "markdown"),
    ]
    for nm, ext, data, want in cases:
        got = K.kind_for(nm, ext, data)
        check("%-28s → %s" % (nm[:28], want), got == want, got)
    check("text 能看能改、asset 只存放",
          _db.caps_for("text")["edit"] and not _db.caps_for("text")["store_only"]
          and _db.caps_for("asset") == {"open": False, "edit": False,
                                        "search": False, "store_only": True},
          _db.caps_for("asset"))

finally:
    print("⑧ 收摊：删掉 zz- 临时物")
    gone = 0
    for fid in [x for x in made if x]:
        try:
            st, _ = call("/api/files/" + fid, "DELETE")
            gone += 1 if st < 400 else 0
        except Exception:
            pass
    print("  删了 %d 篇" % gone)
    left = [fid for fid in [x for x in made if x] if call("/api/files/" + fid)[0] != 404]
    check("临时物一个不剩（逐个按 id 查，404 才算真删）", not left, left[:3])

print("=" * 70)
print("通过" if ok_all else "有失败")
sys.exit(0 if ok_all else 1)
