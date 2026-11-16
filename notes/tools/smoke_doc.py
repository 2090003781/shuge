"""文档格式的验收：上传 docx/odt/rtf/pdf → 解析 → 检索 → 另存为 Markdown。

用**合成文件**（自己按 OOXML/ODF/RTF 规范拼最小结构），不依赖外部样例。
"""
import base64
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
OK = True
MADE = []


def call(path, method="GET", body=None, timeout=300):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:150] if extra else ""))


DOCX_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <w:body>
  <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>项目周报</w:t></w:r></w:p>
  <w:p><w:r><w:t>这周做了</w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>三件事</w:t></w:r>
       <w:r><w:t>，其中一件是</w:t></w:r><w:r><w:t>修了向量缺口</w:t></w:r><w:r><w:t>。</w:t></w:r></w:p>
  <w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>下周计划</w:t></w:r></w:p>
  <w:p><w:r><w:t>继续做编辑器。</w:t></w:r></w:p>
  <w:tbl>
    <w:tr><w:tc><w:p><w:r><w:t>模块</w:t></w:r></w:p></w:tc>
          <w:tc><w:p><w:r><w:t>状态</w:t></w:r></w:p></w:tc></w:tr>
    <w:tr><w:tc><w:p><w:r><w:t>检索</w:t></w:r></w:p></w:tc>
          <w:tc><w:p><w:r><w:t>已上线</w:t></w:r></w:p></w:tc></w:tr>
  </w:tbl>
 </w:body>
</w:document>"""


def docx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships/>')
        z.writestr("word/document.xml", DOCX_XML)
        z.writestr("docProps/core.xml",
                   '<?xml version="1.0"?><cp:coreProperties '
                   'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<dc:title>周报样例</dc:title><dc:creator>辞玖</dc:creator></cp:coreProperties>')
    return buf.getvalue()


def odt_bytes() -> bytes:
    content = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
  xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
  xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">
 <office:body><office:text>
  <text:h text:outline-level="1">季度总结</text:h>
  <text:p>收入增长明显，其中新增客户四十二家。</text:p>
  <text:h text:outline-level="2">细节</text:h>
  <text:p>细节如下：续费率提升。</text:p>
 </office:text></office:body></office:document-content>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", content)
    return buf.getvalue()


RTF = (r"""{\rtf1\ansi\deff0
{\fonttbl{\f0 Arial;}}
\f0\fs24 \b Meeting notes\b0\par
Discussed \u20013?\u25991? support and \'e9 accents.\par
}""")


def pdf_bytes() -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Quarterly PDF report", fontsize=14)
    page.insert_text((72, 130), "revenue up 42 percent", fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


def upload(name: str, data: bytes):
    st, r = call("/api/files", "POST",
                 {"name": name, "content_b64": base64.b64encode(data).decode("ascii")})
    if st == 201 and r.get("file_id"):
        MADE.append(r["file_id"])
    return st, r


# 等库就绪（重启后要几十秒预热嵌入模型）
import time
for _i in range(24):
    try:
        files_before = call("/api/health", timeout=20)[1]["stats"]["files"]
        break
    except Exception:
        print("   （等库就绪… %d）" % (_i + 1))
        time.sleep(5)
else:
    print("❌ 库一直没起来")
    sys.exit(1)
try:
    print("① docx：解析 → 检索 → 转 Markdown")
    st, r = upload("zz-文档-周报.docx", docx_bytes())
    check("上传成功", st == 201, r.get("error"))
    fid = r.get("file_id")
    f = call("/api/files/" + fid)[1]["file"]
    check("kind 判成 document", f["kind"] == "document", f["kind"])
    check("默认不进检索库（rag=off）", f.get("rag") == "off", f.get("rag"))
    check("未入检索时没有解析缓存（打开看的时候才解析）", not (f.get("derived") or {}),
          f.get("derived"))
    t = call("/api/files/%s/text" % fid)[1]
    check("照样能打开看：抽出来的是 Markdown 标题", "# 项目周报" in t["text"], (t["text"] or "")[:60])
    check("加粗保留", "**三件事**" in t["text"])
    check("表格转出来", "| 模块 | 状态 |" in t["text"] and "| 检索 | 已上线 |" in t["text"])
    check("文档属性带出来", (t["meta"] or {}).get("title") == "周报样例", t["meta"])
    check("解析器标明", "docx" in (t["parser"] or ""), t["parser"])
    t2 = call("/api/files/%s/text" % fid)[1]
    check("第二次走缓存", t2.get("cached") is True, t2.get("cached"))
    f = call("/api/files/" + fid)[1]["file"]
    check("看过之后有派生摘要（缓存，不等于入库）",
          (f.get("derived") or {}).get("format") == "docx", f.get("derived"))
    s = call("/api/search?q=%s&limit=20" % urllib.parse.quote("修了向量缺口"))[1]
    check("未入检索：搜不到它", not any(h["file_id"] == fid for h in s["hits"]),
          "%d 命中" % len(s["hits"]))
    st, guard = call("/api/files/" + fid, "PUT", {"content": "想用文本覆盖 docx"})
    check("文档不许被文本写回（保护原件）", st == 400 and "文档" in (guard.get("error") or ""),
          guard.get("error"))

    print("①b 手动加入检索库 / 移出")
    st, rg = call("/api/files/%s/rag" % fid, "POST", {"on": True})
    check("加入检索库 → 解析+切块+嵌入", st == 200 and rg.get("rag") == "on" and rg.get("chunks", 0) > 0,
          rg)
    s = call("/api/search?q=%s&limit=20" % urllib.parse.quote("修了向量缺口"))[1]
    check("加入后能检索到（RAG 走通）", any(h["file_id"] == fid for h in s["hits"]),
          "%d 命中" % len(s["hits"]))
    check("health 里露出未入检索计数", call("/api/health")[1]["stats"].get("rag_off") is not None,
          call("/api/health")[1]["stats"].get("rag_off"))
    st, rg2 = call("/api/files/%s/rag" % fid, "POST", {"on": False})
    check("移出检索库 → 块与向量清掉", rg2.get("rag") == "off" and rg2.get("chunks") == 0, rg2)
    s = call("/api/search?q=%s&limit=20" % urllib.parse.quote("修了向量缺口"))[1]
    check("移出后搜不到", not any(h["file_id"] == fid for h in s["hits"]))
    st, rg3 = call("/api/files/%s/rag" % fid, "POST", {"on": True})
    check("再放回检索库（后面几步要用）", rg3.get("chunks", 0) > 0, rg3)
    st, cv = call("/api/files/%s/convert" % fid, "POST", {})
    check("转成 Markdown 成功", st == 201 and cv.get("file_id"), cv.get("error"))
    MADE.append(cv["file_id"])
    md = call("/api/files/" + cv["file_id"])[1]["file"]
    check("新文件是 .md", md["name"].endswith(".md"), md["name"])
    check("正文来自解析结果", "# 项目周报" in md["content"] and "| 检索 | 已上线 |" in md["content"])
    check("标注了来源与解析器", "转换而来" in md["content"] and "docx" in md["content"],
          md["content"][:60])

    print("② odt / rtf / pdf")
    st, r2 = upload("zz-文档-季度.odt", odt_bytes())
    t = call("/api/files/%s/text" % r2["file_id"])[1]
    check("odt 标题", "# 季度总结" in t["text"], (t["text"] or "")[:50])
    check("odt 二级标题", "## 细节" in t["text"])
    st, r3 = upload("zz-文档-会议.rtf", RTF.encode("latin-1"))
    t = call("/api/files/%s/text" % r3["file_id"])[1]
    check("rtf 正文", "Meeting notes" in t["text"] and "accents" in t["text"], (t["text"] or "")[:60])
    check("rtf unicode 转义", "中文" in t["text"], t["text"][:80])
    try:
        st, r4 = upload("zz-文档-报告.pdf", pdf_bytes())
        t = call("/api/files/%s/text" % r4["file_id"])[1]
        check("pdf 文字", "Quarterly PDF report" in t["text"], (t["text"] or "")[:60])
        check("pdf 页数", (t["meta"] or {}).get("pages") == 1, t["meta"])
        check("pdf 解析器", "pdf(" in (t["parser"] or ""), t["parser"])
    except ImportError:
        print("   ⚠️ 没有 PDF 库，跳过 pdf")

    print("③ 坏文档：不崩、报错清楚、不建块")
    st, r5 = upload("zz-文档-坏的.docx", b"this is not a docx at all")
    fid5 = r5.get("file_id")
    f5 = call("/api/files/" + fid5)[1]["file"]
    check("文件还是存下来了", st == 201, r5.get("error"))
    check("kind 仍是 document", f5["kind"] == "document", f5["kind"])
    check("未入检索时连派生记录都没有", not (f5.get("derived") or {}), f5.get("derived"))
    t5 = call("/api/files/%s/text" % fid5)[1]
    check("取正文时如实报错", t5["error"] and t5["chars"] == 0, t5["error"][:80])
    f5b = call("/api/files/" + fid5)[1]["file"]
    check("看过之后：派生记录里也记着错误",
          "docx" in ((f5b.get("derived") or {}).get("error") or ""),
          (f5b.get("derived") or {}).get("error"))
    print("④ 库内转换矩阵（md → docx/odt/rtf/html/pdf，带产物校验）")
    for to in ("docx", "odt", "rtf", "html"):
        st, cv2 = call("/api/files/%s/convert" % cv["file_id"], "POST", {"to": to})
        check("%s 转换成功" % to, st == 201 and cv2.get("file_id"), cv2.get("error"))
        if st != 201:
            continue
        MADE.append(cv2["file_id"])
        v = cv2.get("verify") or {}
        check("%s 产物校验通过（结构 + 汉字数对照）" % to, v.get("ok") is True,
              "%s | %s" % (v.get("engine_check"), v.get("detail")))
        check("%s 用了成熟引擎" % to, "pandoc" in (cv2.get("engine") or ""), cv2.get("engine"))
        set_size = call("/api/files/" + cv2["file_id"])[1]["file"]["size"]
        check("%s 文件进库且非空" % to, set_size > 0, "%s B" % set_size)
        if to == "docx":
            back = call("/api/files/%s/text" % cv2["file_id"])[1]
            check("docx 能被读回 Markdown（pandoc 优先）",
                  "项目周报" in (back.get("text") or ""), (back.get("text") or "")[:60])
            check("读回时表格还在（gfm 管道表）", "| 模块 |" in (back.get("text") or ""),
                  (back.get("text") or "")[:160])
    st, cvp = call("/api/files/%s/convert" % cv["file_id"], "POST", {"to": "pdf"})
    check("pdf 转换成功（pandoc 出 docx + WPS 另存）", st == 201 and cvp.get("file_id"),
          cvp.get("error"))
    if st == 201:
        MADE.append(cvp["file_id"])
        v = cvp.get("verify") or {}
        check("pdf 可抽文字且汉字数对得上", v.get("ok") is True, v.get("detail"))
        check("pdf 引擎是 pandoc+WPS", "WPS" in (cvp.get("engine") or ""), cvp.get("engine"))
    st, bad = call("/api/files/%s/convert" % cv["file_id"], "POST", {"to": "xyz"})
    check("不支持的格式报 400", st == 400, bad.get("error"))

    print("⑤ 新建文档：给文本 + 文档扩展名 → 直接生成真文档")
    src = "# 新建周报\n\n这条是新建出来的正文。\n"
    st, nd = call("/api/files", "POST", {"name": "zz-新建-周报.docx", "content": src})
    check("新建 .docx 成功", st == 201 and nd.get("file_id"), nd.get("error"))
    gen = (nd or {}).get("generated") or {}
    check("返回里标了生成格式与引擎", gen.get("to") == "docx" and "pandoc" in (gen.get("engine") or ""),
          gen.get("engine"))
    check("生成产物校验通过", (gen.get("verify") or {}).get("ok") is True,
          (gen.get("verify") or {}).get("engine_check"))
    if st == 201:
        MADE.append(nd["file_id"])
        nf = call("/api/files/" + nd["file_id"])[1]["file"]
        check("生成的是真 docx（不是纯文本占了名字）", nf["kind"] == "document"
              and nf["size"] > 300, "%s B / %s" % (nf["size"], nf["kind"]))
        nt = call("/api/files/%s/text" % nd["file_id"])[1]
        check("刚新建的 docx 就能解析出正文", "新建周报" in (nt.get("text") or ""),
              (nt.get("text") or "")[:60])
        st2, back = call("/api/files/%s/convert" % nd["file_id"], "POST", {"to": "md"})
        check("新建的 docx 能另存回 Markdown", st2 == 201 and back.get("file_id"), back.get("error"))
        if st2 == 201:
            MADE.append(back["file_id"])
            check("内容一致", "这条是新建出来的正文" in
                  call("/api/files/" + back["file_id"])[1]["file"]["content"])

    st, nd2 = call("/api/files", "POST", {"name": "zz-新建-报表.pdf", "content": src})
    check("新建 .pdf 成功（pandoc 出 docx + WPS 另存）", st == 201 and nd2.get("file_id"),
          nd2.get("error"))
    if st == 201:
        MADE.append(nd2["file_id"])
        g2 = nd2.get("generated") or {}
        check("pdf 引擎是 pandoc+WPS", "WPS" in (g2.get("engine") or ""), g2.get("engine"))
        check("pdf 产物可抽文字", (g2.get("verify") or {}).get("ok") is True,
              (g2.get("verify") or {}).get("detail"))

    st, nd3 = call("/api/files", "POST", {"name": "zz-新建-普通.md", "content": src})
    check("普通 .md 不走生成（原样落盘）", st == 201 and not (nd3 or {}).get("generated"),
          (nd3 or {}).get("generated"))
    if st == 201:
        MADE.append(nd3["file_id"])

    st, up = upload("zz-文档-二进制上传.docx", docx_bytes())
    check("二进制上传不被重新生成（原字节进库）",
          st == 201 and not (up or {}).get("generated") and up.get("file_id"), up.get("error"))
    if st == 201:
        check("上传的 docx 默认也不进检索库",
              call("/api/files/" + up["file_id"])[1]["file"].get("rag") == "off")
        tt = call("/api/files/%s/text" % up["file_id"])[1]
        check("上传的 docx 解析器正常（看的时候才解析）", "docx" in (tt.get("parser") or ""),
              tt.get("parser"))

finally:
    print("⑥ 清场")
    for fid in MADE:
        call("/api/files/" + fid, "DELETE")
    import sqlite3
    c = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "data", "index.db"))
    n = c.execute("DELETE FROM derived WHERE file_id NOT IN (SELECT id FROM files)").rowcount
    c.execute("DELETE FROM tombstones WHERE display LIKE 'zz-%'")
    c.commit()
    c.close()
    if n:
        print("   （顺手清了 %d 条孤儿派生缓存）" % n)
    after = call("/api/health")[1]["stats"]["files"]
    check("文件数复原", after == files_before, "%s → %s" % (files_before, after))

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
