# -*- coding: utf-8 -*-
"""PDF 引擎"擦字重画 + 一批基础能力"的**离线**验收（不启库、不碰真库、不联网）。

  python tools/acc_pdfeng_edit.py

为什么单独一个：这些是引擎层的行为（字体有没有嵌进去、字有没有真的换掉、
水印是不是斜的、合并后页数对不对），对着 API 跑反而绕远；临时 PDF 全是现场造的。
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import fitz            # noqa: E402
import pdfeng          # noqa: E402

OK, BAD = [], []


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name, ('  | ' + str(extra)[:150]) if extra else ''))


def make_pdf(pages=2):
    """造一篇：第 1 页有灰底纹行 / 中文行 / 英文行 / 一张图，第 2 页有一行同样的词。"""
    doc = fitz.open()
    img = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 40))
    img.set_rect(img.irect, (200, 40, 40))
    for i in range(pages):
        pg = doc.new_page(width=595, height=842)
        pg.draw_rect(fitz.Rect(50, 50, 545, 92), color=None, fill=(0.92, 0.92, 0.92))
        pg.insert_text((60, 80), '第一行：原始文字要被换掉（第 %d 页）' % (i + 1),
                       fontname='china-s', fontsize=14)
        pg.insert_text((60, 130), '第二行：Hello World 特殊词山海经计划',
                       fontname='china-s', fontsize=13)
        pg.insert_text((60, 170), '第三行：这一行一个字都不动，用来当对照。',
                       fontname='china-s', fontsize=12)
        pg.insert_text((60, 200), 'Fourth line: keep this latin line.', fontname='helv', fontsize=12)
        if i == 0:
            pg.insert_image(fitz.Rect(400, 600, 520, 680), pixmap=img)
    return doc


def reopen(doc):
    return fitz.open(stream=doc.tobytes(garbage=4, deflate=True), filetype='pdf')


def text_of(doc, n=1):
    return doc.load_page(n - 1).get_text('text')


def embedded(doc, n=1):
    return [f for f in doc.load_page(n - 1).get_fonts() if f[1] not in ('n/a', '')]


tmp = tempfile.mkdtemp(prefix='zz-pdfedit-')
print('临时目录：%s\n' % tmp)

try:
    print('① 引擎只读模型：行/span 带 origin 与 color（重画要用基线）')
    doc = make_pdf()
    pg = doc.load_page(0)
    lines = pdfeng.page_lines(pg)
    sp = lines[0]['spans'][0]
    chk('page_lines 拿到行与 span', len(lines) >= 4, len(lines))
    chk('span 带 origin（基线）/ size / font / color',
        len(sp.get('origin') or []) == 2 and sp.get('size') and sp.get('font') and sp.get('color') is not None,
        {k: sp.get(k) for k in ('origin', 'size', 'font', 'color')})
    chk('第一行是中文、第三行是对照行',
        '原始文字' in lines[0]['text'] and '对照' in ''.join(l['text'] for l in lines), lines[0]['text'][:20])

    print('\n② 擦字重画：换一行（底纹要留、字要真换、字体要真嵌）')
    r = pdfeng.apply_text_ops(doc, [{"page": 1, "rect": lines[0]['bbox'],
                                     "text": '第一行：换成新写的字', "shrink": True}])
    chk('apply_text_ops 成功', r.get('ok') is True, r.get('error') or r.get('made'))
    d2 = reopen(doc)
    t = text_of(d2)
    chk('新字在文字层里、旧字没了', '换成新写的字' in t and '原始文字' not in t, repr(t[:40]))
    chk('对照行还在（只动了指定那一行）', '一个字都不动' in t, repr(t[-40:]))
    chk('另一行（英文行）也没被动', 'keep this latin line' in t, '')
    emb = embedded(d2)
    chk('新字体真的嵌进去了（ext=ttf，不是 n/a）',
        any(f[1] == 'ttf' for f in emb), emb)
    chk('嵌入的是小子集（不是把 16MB 的字体塞进去）',
        0 < len(d2.tobytes()) < 400000, '%d 字节' % len(d2.tobytes()))
    # 底纹还在吗：擦掉的字那块像素应该还是灰的
    p = d2.load_page(0).get_pixmap(dpi=72)
    px = p.pixel(300, 72)
    chk('底纹（灰色背景）留着没被涂白', abs(px[0] - 234) < 12, px)
    chk('新字真画出来了（那一块有墨）',
        any(p.pixel(x, 70)[0] < 120 for x in range(60, 200, 3)), '')
    d2.close()

    print('\n③ 新字比原位长：自动缩字号；关掉 shrink 要如实标 overflow')
    doc = make_pdf()
    pg = doc.load_page(0)
    ln = pdfeng.page_lines(pg)[0]
    r = pdfeng.apply_text_ops(doc, [{"page": 1, "rect": ln['bbox'],
                                     "text": '第一行：换成一句特别特别长的新文字，长到肯定放不下原位',
                                     "shrink": True}])
    m = (r.get('made') or [{}])[0]
    chk('自动缩了字号（比原来的 14 小）', m.get('size', 99) < 14, m.get('size'))
    chk('缩过之后放得下就不该报 overflow', m.get('overflow') is False, m)
    doc.close()

    doc = make_pdf()
    ln = pdfeng.page_lines(doc.load_page(0))[0]
    r = pdfeng.apply_text_ops(doc, [{"page": 1, "rect": ln['bbox'],
                                     "text": '第一行：不长不短刚好', "shrink": False}])
    m = (r.get('made') or [{}])[0]
    chk('shrink=False 时字号不动（原样 14）', abs(float(m.get('size', 0)) - 14.0) < 0.01, m.get('size'))
    doc.close()

    doc = make_pdf()
    ln = pdfeng.page_lines(doc.load_page(0))[0]
    r = pdfeng.apply_text_ops(doc, [{"page": 1, "rect": ln['bbox'], "shrink": False,
                                     "text": '第一行：' + '这一句长到怎么都放不下原位，必须如实告诉我' * 3}])
    m = (r.get('made') or [{}])[0]
    chk('不缩又真放不下 → 回执里标 overflow（界面要能提醒）', m.get('overflow') is True, m)
    doc.close()

    print('\n④ 全篇查找替换：整行重画，不动别的行')
    doc = make_pdf(pages=3)
    r = pdfeng.find_replace(doc, '山海经计划', '山海经工程')
    chk('find_replace 命中 3 处（3 页各一行）', r.get('count') == 3, r.get('count'))
    d3 = reopen(doc)
    for i in (1, 2, 3):
        t = text_of(d3, i)
        chk('第 %d 页换好了、旧词没了' % i, '山海经工程' in t and '山海经计划' not in t, repr(t[13:40]))
    chk('替换后还是文字（不是烤成图）', 'Hello World' in text_of(d3, 1), '')
    chk('替换也把字体嵌进去了', any(f[1] == 'ttf' for f in embedded(d3)), embedded(d3))
    d3.close()
    doc.close()

    doc = make_pdf()
    r = pdfeng.find_replace(doc, '不存在的词', 'x')
    chk('找不到就不改（count=0 且说清）', r.get('count') == 0 and '没找到' in str(r.get('why')), r.get('why'))
    doc.close()

    print('\n⑤ 插入文字：写到空白处')
    doc = make_pdf()
    r = pdfeng.insert_text(doc, 1, 60, 300, '这是新插进去的一行字', size=13)
    chk('insert_text 成功且嵌了字体', r.get('ok') and r.get('embedded') is True, r)
    d4 = reopen(doc)
    chk('插进去的字读得到', '新插进去的一行字' in text_of(d4), repr(text_of(d4)[:60]))
    d4.close()
    doc.close()

    print('\n⑥ 页码 / 水印')
    doc = make_pdf(pages=3)
    r = pdfeng.stamp_page_numbers(doc, start=1)
    chk('三页都盖了页码', r.get('stamped') == 3, r)
    d5 = reopen(doc)
    chk('第 2 页读得到「第 2 页 / 共 3 页」', '第 2 页 / 共 3 页' in text_of(d5, 2), repr(text_of(d5, 2)[-24:]))
    d5.close()
    doc.close()

    doc = make_pdf(pages=2)
    r = pdfeng.stamp_watermark(doc, '内部资料', size=60, opacity=0.15, angle=45)
    chk('两页都盖了水印', r.get('stamped') == 2, r)
    d6 = reopen(doc)
    # 水印是**图**（PyMuPDF 旋转文字会丢字，如实记）：看它有没有贴上去、是不是半透明、是不是斜的
    chk('水印贴上了（页面里多了张图）', bool(d6.load_page(0).get_images()), d6.load_page(0).get_images())
    chk('水印如实标明是图（不是文字层）', r.get('as_image') is True and '图' in str(r.get('note')), r.get('note'))
    pix = d6.load_page(0).get_pixmap(dpi=100)
    pts = [(x, y) for y in range(0, pix.height, 4) for x in range(0, pix.width, 4)
           if pix.pixel(x, y)[0] < 235]
    span = (max(y for _, y in pts) - min(y for _, y in pts)) if pts else 0
    chk('水印是斜着盖的（墨迹纵向跨度远大于字号）', span > 60, '%d px' % span)
    light = [pix.pixel(x, y)[0] for x, y in pts[:400]]
    chk('是半透明的（不是一块实心黑/灰）', light and min(light) > 60, min(light) if light else None)
    d6.close()
    doc.close()

    print('\n⑦ 合并 / 导出图片 / 扁平化')
    a = make_pdf(pages=2)
    b = make_pdf(pages=3)
    r = pdfeng.merge_pdf(a, b.tobytes(), where='end')
    chk('合并：2 + 3 = 5 页', r.get('ok') and r.get('pages') == 5, r)
    d7 = reopen(a)
    chk('合并后第 5 页是 b 的第 3 页内容', '第 3 页' in text_of(d7, 5), repr(text_of(d7, 5)[:24]))
    r2 = pdfeng.merge_pdf(d7, make_pdf(pages=1).tobytes(), where='start')
    chk('插到最前面：5 + 1 = 6 页', r2.get('pages') == 6, r2)
    d7.close()
    b.close(); a.close()

    doc = make_pdf(pages=2)
    r = pdfeng.export_images(doc, dpi=100)
    chk('导出 2 页图片', r.get('ok') and r.get('count') == 2, r.get('count'))
    chk('导出的是真 PNG（魔数对）', all(i['data'][:4] == b'\x89PNG' for i in r['images']),
        [i['data'][:4] for i in r['images']])
    chk('图片有宽高', all(i['w'] > 100 and i['h'] > 100 for i in r['images']), [(i['w'], i['h']) for i in r['images']])
    chk('一次要太多页会被拦住（cap）', pdfeng.export_images(make_pdf(pages=1), pages=list(range(1, 40))).get('ok') is False, '')
    doc.close()

    doc = make_pdf(pages=2)
    doc.load_page(0).add_highlight_annot(pdfeng.page_lines(doc.load_page(0))[0]['bbox'] and
                                         fitz.Rect(pdfeng.page_lines(doc.load_page(0))[0]['bbox']))
    chk('先加上一条批注', len(list(doc.load_page(0).annots())) == 1, len(list(doc.load_page(0).annots())))
    r = pdfeng.bake_annots(doc, dpi=120)
    d8 = reopen(doc)
    chk('扁平化后批注没了', r.get('pages') == 1 and len(list(d8.load_page(0).annots())) == 0, r)
    chk('扁平化后这页有图（烤成图了）', bool(d8.load_page(0).get_images()), d8.load_page(0).get_images())
    chk('扁平化会如实说明"这页不再是文字"', '不再是文字' in str(r.get('note')), r.get('note'))
    d8.close(); doc.close()

    print('\n⑧ 同一页连着画两种字体（2026-10-01 踩过的坑：字体名撞了 → 文字层变 \\x00）')
    doc = make_pdf(pages=1)                             # 一页，页码才是「共 1 页」
    ln = pdfeng.page_lines(doc.load_page(0))[0]
    pdfeng.apply_text_ops(doc, [{"page": 1, "rect": ln['bbox'], "text": '第一次改的中文'}])
    pdfeng.stamp_page_numbers(doc, start=1)             # 页码走的是另一套字体（雅黑）
    d9 = reopen(doc)
    t9 = text_of(d9)
    chk('两种字体先后画，两种字都在文字层里（没有 \\x00）',
        '第一次改的中文' in t9 and '第 1 页 / 共 1 页' in t9 and '\x00' not in t9, repr(t9[-40:]))
    d9.close(); doc.close()

    print('\n⑨ 该拒绝的')
    doc = make_pdf()
    chk('页码越界 → 说清共几页', '越界' in str(pdfeng.apply_text_ops(doc, [{"page": 9, "rect": [1, 1, 9, 9], "text": 'x'}]).get('error')), '')
    chk('rect 不合法 → 400 口径的人话', 'rect' in str(pdfeng.apply_text_ops(doc, [{"page": 1, "rect": [1, 2], "text": 'x'}]).get('error')), '')
    chk('空 ops → 说清没东西改', '空的' in str(pdfeng.apply_text_ops(doc, []).get('error')), '')
    chk('替换不给 q → 说清', 'q 不能空' in str(pdfeng.find_replace(doc, '', 'x').get('error')), '')
    chk('水印不给字 → 说清', 'watermark' in str(pdfeng.stamp_watermark(doc, ' ').get('error')), '')
    chk('合并给了垃圾字节 → 说清打不开', pdfeng.merge_pdf(doc, b'not a pdf').get('ok') is False, '')
    doc.close()
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

print('\n通过 %d / %d；失败：%s' % (len(OK), len(OK) + len(BAD), '、'.join(BAD) if BAD else '无'))
sys.exit(1 if BAD else 0)
