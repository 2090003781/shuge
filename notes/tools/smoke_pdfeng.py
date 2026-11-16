# -*- coding: utf-8 -*-
"""PDF 引擎的 API 验收（**临时库**，真库完全不碰）：

  python tools/smoke_pdfeng.py

验的是"引擎那头"：`GET /pdf`（页/元信息/批注/扫描件）、`GET /pdf/text`（文字层）、
`POST /pdf/annot`（加/删批注 → 写回，留前像）、`POST /pdf/pages`（旋转/删页/提取），
外加几条该拒绝的（乱 op、没东西改、对非 PDF 调）。

为什么对着临时库跑：这一套会**真的生成 PDF 并改写它**（要 WPS），真库上跑一次就动一次主人的文件。
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
import urllib.parse
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
                                        "X-Writer": "tool:pdfeng-acc"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            b = resp.read()
            return resp.status, (b if raw else json.loads(b.decode('utf-8', 'replace') or '{}'))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode('utf-8', 'replace') or '{}')
        except Exception:
            return e.code, {}


def main() -> int:
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    data = tempfile.mkdtemp(prefix="zz-pdfeng-")
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

        print('① 造一篇多页 PDF（WPS 现场生成）')
        body = ('# 引擎验收\n\n第一段：这一行要能被文字层抽出来。\n\n'
                + '\n\n'.join('## 第 %d 节\n\n%s' % (i, '撑页面的正文。' * 60) for i in range(1, 6)))
        st, d = req(base, "/api/files", {"name": "zz-引擎.pdf", "content": body})
        fid = d.get("file_id")
        chk('建 PDF -> 201', st == 201 and bool(fid), d)

        print('\n② GET /pdf：引擎总入口')
        st, d = req(base, "/api/files/%s/pdf" % fid)
        chk('200 且 ok、页数 ≥2', st == 200 and d.get("ok") and d.get("count", 0) >= 2, d.get("count"))
        chk('带每页宽高/字数/旋转', bool(d.get("pages")) and d["pages"][0].get("w", 0) > 0, d.get("pages", [])[:1])
        chk('认得出不是扫描件', d.get("scanned") is False, d.get("scanned"))
        chk('一开始没有批注', d.get("annots") == [], d.get("annots"))

        print('\n③ GET /pdf/text：文字层（行 + span + bbox）')
        st, tl = req(base, "/api/files/%s/pdf/text?n=1" % fid)
        lines = tl.get("lines") or []
        chk('200 且抽到行', st == 200 and len(lines) >= 1, len(lines))
        sp = (lines[0].get("spans") or [{}])[0] if lines else {}
        chk('span 带 bbox / 字号 / 字体', len(sp.get("bbox") or []) == 4 and sp.get("font"), sp)
        chk('页码越界会说清', '越界' in str(req(base, "/api/files/%s/pdf/text?n=99" % fid)[1].get("error")), '')

        print('\n④ POST /pdf/annot：加高亮 → 写回（留前像）')
        rects = [(lines[0].get("bbox") if lines else [90, 90, 200, 110])]
        st, d = req(base, "/api/files/%s/pdf/annot" % fid,
                    {"add": [{"page": 1, "kind": "highlight", "rects": rects,
                              "text": "引擎验收：高亮", "color": "yellow"}]})
        chk('加批注 -> 200 且 ok', st == 200 and d.get("ok") is True, d)
        chk('留了前像（可回退）', bool(d.get("before_image")), d.get("before_image"))
        chk('回执里就说清了写回模式', d.get("mode") in ("incremental", "full"), d.get("mode"))
        st, d2 = req(base, "/api/files/%s/pdf" % fid)
        chk('再查 /pdf：批注在里面（1 条，第 1 页，highlight）',
            len(d2.get("annots") or []) == 1 and d2["annots"][0]["page"] == 1
            and d2["annots"][0]["kind"] == "highlight", d2.get("annots"))

        print('\n⑤ 再画一条便签 + 删掉第一条')
        st, d3 = req(base, "/api/files/%s/pdf/annot" % fid,
                     {"add": [{"page": 1, "kind": "note", "rects": rects, "text": "便签内容"}]})
        chk('便签也能加', st == 200 and d3.get("ok") is True, d3.get("added"))
        st, d4 = req(base, "/api/files/%s/pdf" % fid)
        ids = [a["id"] for a in (d4.get("annots") or [])]
        chk('现在 2 条批注', len(ids) == 2, ids)
        st, d5 = req(base, "/api/files/%s/pdf/annot" % fid, {"del": [ids[0]]})
        chk('删掉一条 -> 200', st == 200 and d5.get("ok") is True and d5.get("deleted") == 1, d5)
        st, d6 = req(base, "/api/files/%s/pdf" % fid)
        chk('删完只剩 1 条', len(d6.get("annots") or []) == 1, len(d6.get("annots") or []))
        keep = (d6.get("annots") or [{}])[0]
        chk('便签读回来还是 note（不是 PyMuPDF 的类型名 text）+ 带着内容',
            keep.get("kind") == "note" and keep.get("content") == "便签内容", keep)
        st, d6b = req(base, "/api/files/%s/pdf/annot" % fid,
                      {"add": [{"page": 1, "kind": "rect", "rects": rects, "color": "blue"}]})
        st, d6c = req(base, "/api/files/%s/pdf" % fid)
        chk('矩形框读回来是 rect（PyMuPDF 的 Square 也归 rect）',
            st == 200 and any(a.get("kind") == "rect" for a in (d6c.get("annots") or [])),
            [a.get("kind") for a in (d6c.get("annots") or [])])

        print('\n⑥ POST /pdf/pages：旋转 / 删页 / 提取')
        st, d7 = req(base, "/api/files/%s/pdf/pages" % fid, {"op": "rotate", "n": 1})
        chk('转 90 度', st == 200 and d7.get("ok") is True, d7)
        st, d8 = req(base, "/api/files/%s/pdf" % fid)
        chk('再查 /pdf：第 1 页 rotation=90', d8["pages"][0]["rotation"] == 90, d8["pages"][0])
        n_before = d8["count"]
        st, d9 = req(base, "/api/files/%s/pdf/pages" % fid, {"op": "delete", "n": n_before})
        chk('删最后一页', st == 200 and d9.get("ok") is True and d9.get("count") == n_before - 1, d9)
        st, d10 = req(base, "/api/files/%s/pdf/pages" % fid, {"op": "extract", "nums": [1]})
        chk('提取第 1 页 → 新的一篇', st == 200 and d10.get("ok") is True and d10.get("file_id"), d10)
        chk('提取出来的也是 PDF（能查 /pdf）',
            req(base, "/api/files/%s/pdf" % d10.get("file_id"))[1].get("ok") is True, '')

        print('\n⑦ 擦字重画：POST /pdf/text-edit')
        st, tl = req(base, "/api/files/%s/pdf/text?n=1" % fid)
        ln1 = (tl.get("lines") or [{}])[0]
        st, dt = req(base, "/api/files/%s/pdf/text-edit" % fid,
                     {"ops": [{"page": 1, "rect": ln1.get("bbox"),
                               "text": "引擎验收：这一行是重画过的"}]})
        chk('改一行 -> 200 且 ok', st == 200 and dt.get("ok") is True, dt.get("error") or dt.get("changed"))
        chk('回执里说清改了几处、字体嵌没嵌',
            dt.get("changed") == 1 and (dt.get("made") or [{}])[0].get("embedded") is True,
            dt.get("made"))
        st, tl2 = req(base, "/api/files/%s/pdf/text?n=1" % fid)
        allt = " ".join((l.get("text") or "") for l in (tl2.get("lines") or []))
        oldlines = [l.get("text") for l in (tl2.get("lines") or [])]
        chk('新字在文字层里、原来那一行不再是原来那句（旧行文本是新文本的前缀，不能拿前缀当判据）',
            "重画过的" in allt and (ln1.get("text") or "") not in oldlines, oldlines[:4])
        st, dtx = req(base, "/api/files/%s/pdf/text-edit" % fid, {"ops": []})
        chk('空 ops -> 400 说清', st == 400 and "ops" in str(dtx.get("error")), dtx.get("error"))

        print('\n⑧ 全篇查找替换：POST /pdf/replace')
        st, dr = req(base, "/api/files/%s/pdf/replace" % fid, {"q": "重画过的", "rep": "替换过的"})
        chk('替换 -> 200 且命中 ≥1', st == 200 and (dr.get("count") or 0) >= 1, dr.get("count"))
        st, tl3 = req(base, "/api/files/%s/pdf/text?n=1" % fid)
        allt = " ".join((l.get("text") or "") for l in (tl3.get("lines") or []))
        chk('替换后文字层是新的', "替换过的" in allt and "重画过的" not in allt, allt[:60])
        st, dz = req(base, "/api/files/%s/pdf/replace" % fid, {"q": "库里绝对没有的词", "rep": "x"})
        chk('没命中：200 且 count=0 + 说清（不写回）',
            st == 200 and dz.get("count") == 0 and "没找到" in str(dz.get("why")), dz)
        st, dq = req(base, "/api/files/%s/pdf/replace" % fid, {"rep": "x"})
        chk('不给 q -> 400', st == 400, dq.get("error"))

        print('\n⑨ 印记：页码 / 水印')
        st, dp = req(base, "/api/files/%s/pdf/stamp" % fid, {"kind": "pageno", "start": 1})
        chk('盖页码 -> 200', st == 200 and dp.get("ok") is True, dp.get("error"))
        st, tl4 = req(base, "/api/files/%s/pdf/text?n=1" % fid)
        allt = " ".join((l.get("text") or "") for l in (tl4.get("lines") or []))
        chk('页码进了文字层（搜得到，不是逐字散的）', "共" in allt and "页" in allt, allt[-40:])
        st, dw = req(base, "/api/files/%s/pdf/stamp" % fid,
                     {"kind": "watermark", "text": "验收水印", "opacity": 0.2})
        chk('盖水印 -> 200 且如实说"是图"', st == 200 and dw.get("as_image") is True, dw.get("note"))
        st, dk = req(base, "/api/files/%s/pdf/stamp" % fid, {"kind": "bogus"})
        chk('乱写 kind -> 400 说清两种', st == 400 and "pageno" in str(dk.get("error")), dk.get("error"))

        print('\n⑩ 合并：POST /pdf/merge')
        st, dnew = req(base, "/api/files", {"name": "zz-引擎-待并.pdf",
                                            "content": "# 待并的一篇\n\n" + "并进来的正文。" * 40})
        fid2 = dnew.get("file_id")
        pages_a = req(base, "/api/files/%s/pdf" % fid)[1].get("count")
        pages_b = req(base, "/api/files/%s/pdf" % fid2)[1].get("count")
        st, dm = req(base, "/api/files/%s/pdf/merge" % fid, {"ids": [fid2], "where": "end"})
        chk('合并 -> 200 且页数 = 两篇之和',
            st == 200 and dm.get("pages") == (pages_a or 0) + (pages_b or 0),
            [pages_a, pages_b, dm.get("pages")])
        st, dmd = req(base, "/api/files", {"name": "zz-引擎-文本.md", "content": "# 文本\n"})
        st, dmm = req(base, "/api/files/%s/pdf/merge" % fid, {"ids": [dmd.get("file_id")]})
        chk('拿 md 去并 -> 400 说清不是 PDF', st == 400 and "不是 PDF" in str(dmm.get("error")),
            dmm.get("error"))

        print('\n⑪ 导出图片：POST /pdf/export-images（产出新文件）')
        st, di = req(base, "/api/files/%s/pdf/export-images" % fid, {"nums": [1], "dpi": 100})
        chk('导出第 1 页 -> 200 且给了新文件', st == 200 and (di.get("count") or 0) == 1, di.get("error"))
        nf = (di.get("files") or [{}])[0]
        chk('新文件是真 PNG（读原字节看魔数）',
            req(base, "/api/files/%s/raw" % nf.get("file_id"), raw=True)[1][:4] == b"\x89PNG",
            nf.get("name"))
        st, di2 = req(base, "/api/files/%s/pdf/export-images" % fid, {"nums": list(range(1, 40))})
        chk('一次要太多页 -> 400 拦住', st == 400 and "最多" in str(di2.get("error")), di2.get("error"))

        print('\n⑫ 扁平化：POST /pdf/flatten（把批注烤进内容）')
        req(base, "/api/files/%s/pdf/annot" % fid,
            {"add": [{"page": 1, "kind": "highlight", "rects": [ln1.get("bbox")], "text": "待烤"}]})
        before = len(req(base, "/api/files/%s/pdf" % fid)[1].get("annots") or [])
        st, dfl = req(base, "/api/files/%s/pdf/flatten" % fid, {"dpi": 110})
        after = len(req(base, "/api/files/%s/pdf" % fid)[1].get("annots") or [])
        chk('扁平化 -> 200 且批注没了', st == 200 and before >= 1 and after == 0, [before, after])
        chk('扁平化如实说"这页不再是文字"', "不再是文字" in str(dfl.get("note")), dfl.get("note"))

        print('\n⑬ 该拒绝的')
        st, d11 = req(base, "/api/files/%s/pdf/pages" % fid, {"op": "bogus"})
        chk('乱写 op -> 400 且说清可用哪几个', st == 400 and "rotate" in str(d11.get("error")), d11.get("error"))
        st, d12 = req(base, "/api/files/%s/pdf/annot" % fid, {})
        chk('add/del 都不给 -> 400', st == 400, d12.get("error"))
        st, d13 = req(base, "/api/files/%s/pdf/annot" % fid,
                      {"add": [{"page": 1, "kind": "bogus", "rects": rects}]})
        chk('乱写批注类型 -> 400', st == 400, d13.get("error"))
        st, d14 = req(base, "/api/files", {"name": "zz-文本.md", "content": "# 文本\n"})
        st, d15 = req(base, "/api/files/%s/pdf" % d14.get("file_id"))
        chk('对 md 调 /pdf -> 400「不是 PDF」', st == 400 and "不是 PDF" in str(d15.get("error")), d15.get("error"))
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
