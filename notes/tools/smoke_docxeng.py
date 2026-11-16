# -*- coding: utf-8 -*-
"""DOCX 引擎的 API 验收（**临时库**，真库完全不碰）。

  python tools/smoke_docxeng.py

验的是"引擎接到库里那条路"：GET /docx 读模型、POST /docx/edit 写回（留前像/重解析/缩短闸）、
POST /docx/export 导出新文件（md/txt/html/pdf），外加几条该拒绝的。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OK, BAD = [], []


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name, ('  | ' + str(extra)[:150]) if extra else ''))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def req(base, path, body=None, method=None, timeout=600, raw=False):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json",
                                        "X-Writer": "tool:docxeng-acc"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            b = resp.read()
            return resp.status, (b if raw else json.loads(b.decode('utf-8', 'replace') or '{}'))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode('utf-8', 'replace') or '{}')
        except Exception:
            return e.code, {}


MD = """# 引擎验收标题

第一段：普通正文，带 **加粗**。

## 二级小标题

- 项目一
- 项目二

| 姓名 | 分数 |
|---|---|
| 张三 | 90 |

结尾段落：这一句要留着对照。

正文再铺一段，好让"缩短闸"（原文 ≥400 字才生效）真的被触发：这一句是故意写长的凑字句，用来把解析出来的正文字数顶过四百。这一句是故意写长的凑字句，用来把解析出来的正文字数顶过四百。这一句是故意写长的凑字句，用来把解析出来的正文字数顶过四百。这一句是故意写长的凑字句，用来把解析出来的正文字数顶过四百。这一句是故意写长的凑字句，用来把解析出来的正文字数顶过四百。这一句是故意写长的凑字句，用来把解析出来的正文字数顶过四百。这一句是故意写长的凑字句，用来把解析出来的正文字数顶过四百。这一句是故意写长的凑字句，用来把解析出来的正文字数顶过四百。这一句是故意写长的凑字句，用来把解析出来的正文字数顶过四百。这一句是故意写长的凑字句，用来把解析出来的正文字数顶过四百。这一句是故意写长的凑字句，用来把解析出来的正文字数顶过四百。这一句是故意写长的凑字句，用来把解析出来的正文字数顶过四百。
"""


def main() -> int:
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    data = tempfile.mkdtemp(prefix="zz-docxacc-")
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "src", "server.py"),
                            "--port", str(port), "--data", data], cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        t0 = time.time()
        while time.time() - t0 < 90:
            if req(base, "/api/health", timeout=5)[0] == 200:
                break
            time.sleep(1)
        print('临时库就绪 %.1fs\n' % (time.time() - t0))

        print('① 造一篇 docx（pandoc 现场生成）')
        st, d = req(base, "/api/files", {"name": "zz-引擎.docx", "content": MD})
        fid = d.get("file_id")
        chk('建 docx -> 201', st == 201 and fid, {k: d.get(k) for k in ("ok", "generated")})
        if not fid:
            return 1

        print('\n② GET /docx：读模型')
        st, m = req(base, "/api/files/%s/docx" % fid)
        chk('200 且 ok', st == 200 and m.get("ok") is True, m.get("error"))
        bl = m.get("blocks") or []
        chk('块模型有段落/表格', any(b["kind"] == "p" for b in bl) and any(b["kind"] == "table" for b in bl),
            [b["kind"] for b in bl][:8])
        chk('标题层级认出来了', any((b.get("heading") or 0) == 1 for b in bl),
            [(b.get("heading"), (b.get("text") or "")[:8]) for b in bl[:4]])
        chk('表格单元格读到了', any("张三" in str(b.get("cells")) for b in bl if b["kind"] == "table"), '')
        chk('info 报出段/表/标题大纲', (m.get("info") or {}).get("paras", 0) >= 4
            and (m.get("info") or {}).get("tables") == 1, m.get("info"))
        chk('顺手给了渲染好的 HTML', "<h1" in (m.get("html") or ""), (m.get("html") or "")[:40])

        print('\n③ POST /docx/edit：改字（保样式）+ 留前像')
        i_end = [b["i"] for b in bl if "对照" in (b.get("text") or "")][0]
        i_h1 = [b["i"] for b in bl if (b.get("heading") or 0) == 1][0]
        st, e = req(base, "/api/files/%s/docx/edit" % fid,
                    {"ops": [{"op": "text", "i": i_end, "text": "结尾段落：被引擎改过了。"}]})
        chk('改一段 -> 200 且 ok', st == 200 and e.get("ok") is True, e.get("error") or e.get("changed"))
        chk('留了前像（可回退）', bool(e.get("before_image")), e.get("before_image"))
        chk('回执里有引擎名与体积', e.get("engine") == "docxeng" and (e.get("bytes") or 0) > 1000, e.get("bytes"))
        st, m2 = req(base, "/api/files/%s/docx" % fid)
        bl2 = m2.get("blocks") or []
        chk('改的那段变了', any("被引擎改过了" in (b.get("text") or "") for b in bl2), '')
        chk('标题还在、还是标题（样式没被 pandoc 洗掉）',
            any((b.get("heading") or 0) == 1 for b in bl2), '')
        chk('表格还在、内容没动', any(b["kind"] == "table" and "张三" in str(b.get("cells")) for b in bl2), '')
        st, mt = req(base, "/api/files/%s/text" % fid)
        chk('解析出来的正文里也是新文字（重解析生效了）',
            "被引擎改过了" in (mt.get("text") or ""), (mt.get("text") or "")[:40])

        print('\n④ 样式 / 结构 / 表格 / 替换 一起上')
        st, e2 = req(base, "/api/files/%s/docx/edit" % fid, {"ops": [
            {"op": "run", "i": i_end, "j": 0, "props": {"b": True, "size": 16}},
            {"op": "para", "i": i_end, "props": {"align": "center", "style": "Heading 2"}},
            {"op": "insert", "i": i_h1, "text": "插到最前面的新段落", "style": "Heading 2"},
            {"op": "replace", "q": "张三", "rep": "张大师"},
        ]})
        chk('一批 4 个操作全成功', st == 200 and e2.get("changed") == 4, e2.get("error") or e2.get("changed"))
        st, m3 = req(base, "/api/files/%s/docx" % fid)
        bl3 = m3.get("blocks") or []
        chk('插的段在最前面、成了标题 2',
            "插到最前面的新段落" in (bl3[0].get("text") or "") and (bl3[0].get("heading") or 0) == 2,
            [(b.get("heading"), (b.get("text") or "")[:12]) for b in bl3[:3]])
        chk('替换生效（张三 → 张大师）', "张大师" in str(bl3) and "张三" not in str(bl3), '')
        row = [b for b in bl3 if b["kind"] == "p" and "被引擎改过了" in (b.get("text") or "")]
        chk('改过的段：居中 + 加粗 + 标题 2',
            row and row[0].get("align") == "center" and (row[0].get("heading") or 0) == 2
            and any(r.get("b") for r in (row[0].get("runs") or [])),
            row[0] if row else None)

        print('\n⑤ 该拒绝的 / 缩短闸')
        st, dz = req(base, "/api/files/%s/docx/edit" % fid, {"ops": []})
        chk('空 ops -> 400 说清', st == 400 and "ops" in str(dz.get("error")), dz.get("error"))
        st, dbad = req(base, "/api/files/%s/docx/edit" % fid, {"ops": [{"op": "bogus"}]})
        chk('乱写 op -> 400 并列出可用的', st == 400 and "不认识的 op" in str(dbad.get("error")), dbad.get("error"))
        st, dmd = req(base, "/api/files", {"name": "zz-引擎.md", "content": "# 文本\n"})
        st, dno = req(base, "/api/files/%s/docx" % dmd.get("file_id"))
        chk('对 md 调 /docx -> 400「不是 docx」', st == 400 and "不是 docx" in str(dno.get("error")), dno.get("error"))
        # 缩短闸：把每一段都改成空
        st, m4 = req(base, "/api/files/%s/docx" % fid)
        ops = [{"op": "text", "i": b["i"], "text": ""} for b in (m4.get("blocks") or []) if b["kind"] == "p"]
        st, dsh = req(base, "/api/files/%s/docx/edit" % fid, {"ops": ops})
        chk('砍到几乎空白 -> 被缩短闸拦住（要 allow_shrink 才放行）',
            st == 400 and dsh.get("shrink_refused") is True, dsh.get("error"))

        print('\n⑥ POST /docx/export：导出新文件')
        for to, magic, name_end in (("txt", "引擎验收标题".encode("utf-8"), ".txt"),
                                    ("md", "引擎验收标题".encode("utf-8"), ".md"),
                                    ("html", b"<h1", ".html")):
            st, dx = req(base, "/api/files/%s/docx/export" % fid, {"to": to}, timeout=300)
            ok = st == 200 and dx.get("file_id") and dx.get("name", "").endswith(name_end)
            body = req(base, "/api/files/%s/raw" % dx.get("file_id"), raw=True)[1] if ok else b""
            chk('导出 %s -> 新文件且内容对得上' % to,
                ok and (magic in body or magic.lower() in body.lower()), [dx.get("name"), len(body)])
        st, dp = req(base, "/api/files/%s/docx/export" % fid, {"to": "pdf"}, timeout=900)
        chk('导出 pdf -> 新文件是真 PDF（WPS 直接吃 docx，不经 md）',
            st == 200 and dp.get("file_id") and (dp.get("engine") or "").startswith("WPS")
            and req(base, "/api/files/%s/raw" % dp.get("file_id"), raw=True)[1][:4] == b"%PDF",
            [dp.get("engine"), dp.get("name")])
        st, dbo = req(base, "/api/files/%s/docx/export" % fid, {"to": "bogus"})
        chk('乱写 to -> 400', st == 400, dbo.get("error"))
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            srv.kill()
        shutil.rmtree(data, ignore_errors=True)
        print('\n临时库已杀、目录已删')

    print('\n通过 %d / %d；失败：%s' % (len(OK), len(OK) + len(BAD), '、'.join(BAD) if BAD else '无'))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
