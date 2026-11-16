# -*- coding: utf-8 -*-
"""「按格式切编辑器」的后端验收（**临时库**，真库完全不碰）：

  python tools/smoke_editors.py

验四件事：① PDF 页清单 / 单页渲染成 PNG / 页码越界要说话 ② PDF 全文查找（页码 + 矩形 + 片段）
③ 文档回写 `/writeback`（按原格式重新生成、留前像、结构校验、改完能重新解析）
④ 三道闸：缩短闸（不足一半要 allow_shrink）、文本格式要指路 PUT、非 PDF 调 /pages 要说清。
为什么对着临时库跑：这一套会**真的生成 PDF/docx**（要 WPS/pandoc），也会**覆盖文档**——
真库上跑一次就动一次主人的文件，不值当。
"""
import json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.error, urllib.request

ROOT = r'D:\core-notes'
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
                                        "X-Writer": "tool:editor-acc"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            b = resp.read()
            return resp.status, (b if raw else json.loads(b.decode('utf-8', 'replace') or '{}'))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode('utf-8', 'replace') or '{}')
        except Exception:
            return e.code, {}


port = free_port()
base = "http://127.0.0.1:%d" % port
data = tempfile.mkdtemp(prefix="zz-editors-")
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

    print('① 造一篇 PDF（走 WPS/pandoc 现场生成）')
    md = '# 年度总结\n\n第一段正文，讲的是整体情况。\n\n## 第二节\n\n这里有个特殊词：山海经计划。\n'
    st, d = req(base, '/api/files', {"name": "zz-editor.pdf", "content": md})
    fid = d.get('file_id')
    chk('建 PDF -> 201', st == 201 and bool(fid), json.dumps(d, ensure_ascii=False)[:120])
    if not fid:
        raise SystemExit('没建成 PDF，后面不用测了')

    print('\n② 页清单 /pages')
    st, d = req(base, '/api/files/%s/pages' % fid)
    pg = d.get('pages') or []
    chk('GET /pages -> 200 且 ok', st == 200 and d.get('ok') is True, d)
    chk('页数 ≥1 且带每页字数', bool(pg) and pg[0].get('w', 0) > 0, pg[:1])

    print('\n③ 单页渲染 /page?n=1&w=800')
    st, raw = req(base, '/api/files/%s/page?n=1&w=800' % fid, raw=True)
    chk('GET /page -> 200', st == 200, st)
    chk('返回的是 PNG（魔数）', isinstance(raw, bytes) and raw[:8] == b'\x89PNG\r\n\x1a\n', raw[:8] if isinstance(raw, bytes) else raw)
    chk('图不小（>5KB）', isinstance(raw, bytes) and len(raw) > 5000, len(raw) if isinstance(raw, bytes) else '')
    st, d = req(base, '/api/files/%s/page?n=99&w=800' % fid)
    chk('页码越界 -> 400 人话', st == 400 and '越界' in str(d.get('error')), (st, d.get('error')))

    print('\n④ 全文查找 /find?q=')
    st, d = req(base, '/api/files/%s/find?q=%s' % (fid, urllib.parse.quote('山海经计划')))
    hits = d.get('hits') or []
    chk('找到 1 处以上', st == 200 and d.get('count', 0) >= 1, d.get('count'))
    chk('命中带页码 + 矩形 + 片段',
        bool(hits) and hits[0].get('page') == 1 and len(hits[0].get('rect') or []) == 4
        and '山海经计划' in (hits[0].get('snippet') or ''), hits[:1])
    st, d = req(base, '/api/files/%s/find?q=%s' % (fid, urllib.parse.quote('这词不存在啊')))
    chk('找不到 -> count 0（不是报错）', st == 200 and d.get('count') == 0, d.get('count'))
    st, d = req(base, '/api/files/%s/find' % fid)
    chk('不给 q -> 400', st == 400, st)

    print('\n⑤ 文档回写 /writeback（PDF）')
    st, d = req(base, '/api/files/%s/writeback' % fid,
                {"content": "# 改过的标题\n\n这一版是编辑器里改的，含特殊词：山海经计划。\n"})
    chk('writeback -> 200 且 ok', st == 200 and d.get('ok') is True, d)
    chk('留了前像（可回退）', bool(d.get('before_image')), d.get('before_image'))
    chk('结构校验也回来了', isinstance(d.get('verify'), dict), d.get('verify'))
    st, t = req(base, '/api/files/%s/text' % fid)
    chk('重新解析出改后的正文', '改过的标题' in (t.get('text') or ''), (t.get('text') or '')[:60])

    print('\n⑥ 缩短闸：一句话盖掉一整篇要拦（得用一篇够长的才测得出来）')
    long_md = '# 长文\n\n' + ''.join('第 %d 段：' % i + '这一段用来把篇幅撑过四百字。' * 6 + '\n\n'
                                       for i in range(1, 9))
    st, d = req(base, '/api/files', {"name": "zz-editor-long.pdf", "content": long_md})
    fidL = d.get('file_id')
    st, t = req(base, '/api/files/%s/text' % fidL)
    chk('长 PDF 解析出 400 字以上（不然闸门本来就不该拦）', len((t.get('text') or '').strip()) >= 400,
        len((t.get('text') or '').strip()))
    st, d = req(base, '/api/files/%s/writeback' % fidL, {"content": "# 短\n\n没了。\n"})
    chk('缩太多 -> 400 且带 shrink_refused', st == 400 and d.get('shrink_refused') is True,
        (st, d.get('error')))
    st, d = req(base, '/api/files/%s/writeback' % fidL, {"content": "# 短\n\n没了。\n", "allow_shrink": True})
    chk('带 allow_shrink=true 才放行', st == 200 and d.get('ok') is True, st)

    print('\n⑦ docx 也走同一条路')
    st, d = req(base, '/api/files', {"name": "zz-editor.docx", "content": "# 文档标题\n\n正文一段。\n"})
    fid2 = d.get('file_id')
    chk('建 docx -> 201', st == 201 and bool(fid2), d)
    if fid2:
        st, d = req(base, '/api/files/%s/writeback' % fid2, {"content": "# 文档标题（改）\n\n正文一段改过。\n"})
        chk('docx writeback -> 200', st == 200 and d.get('ok') is True, d)
        st, t = req(base, '/api/files/%s/text' % fid2)
        chk('解析出改后的 docx 正文', '改过' in (t.get('text') or ''), (t.get('text') or '')[:60])

    print('\n⑧ 文本格式不走 writeback（该走 PUT）')
    st, d = req(base, '/api/files', {"name": "zz-editor.md", "content": "# 文本\n"})
    fid3 = d.get('file_id')
    if fid3:
        st, d = req(base, '/api/files/%s/writeback' % fid3, {"content": "# 改\n"})
        chk('对 .md 调 writeback -> 400 并指路 PUT', st == 400 and 'PUT' in str(d.get('error')), (st, d.get('error')))

    print('\n⑨ 非 PDF 调 /pages 要说得清')
    if fid2:
        st, d = req(base, '/api/files/%s/pages' % fid2)
        chk('docx 调 /pages -> 400「不是 PDF」', st == 400 and '不是 PDF' in str(d.get('error')), (st, d.get('error')))
finally:
    srv.terminate()
    try:
        srv.wait(timeout=10)
    except Exception:
        srv.kill()
    shutil.rmtree(data, ignore_errors=True)
    print('\n临时库已杀、目录已删')

print('\n通过 %d / %d；失败：%s' % (len(OK), len(OK) + len(BAD), '、'.join(BAD) if BAD else '无'))
sys.exit(1 if BAD else 0)
