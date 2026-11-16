"""并发/冲突/高频 实测 A 组（无破坏性）：
   ① 权限连点（串行 60 次 + 并发 10 路）
   ② ops/call 并发（只读）
   ③ 档位在变的同时并发调 ops（无副作用的调用）
   ④ 状态/日志接口并发拉 + 高频询问（验证缓存把库挡住了）
   ⑤ 并发建 8 篇临时笔记再并发删掉（真数据安全：单线程库 + sqlite）
"""

import os as _os, sys as _sys          # 门禁用（2026-09-30 体检）

if "--yes" not in _sys.argv and _os.environ.get("COREHOST_ALLOW_DESTRUCTIVE") != "1":
    print(__doc__ or "")
    print("\n【门禁】%s 会真的动到**运行中的枢阁 / 库文件夹**（这个脚本忽略参数，--help 也照跑）。"
          % _os.path.basename(__file__))
    print("       确认要跑：python %s --yes（或设 COREHOST_ALLOW_DESTRUCTIVE=1）"
          % _os.path.basename(__file__))
    _sys.exit(2)
import json
import os
import threading
import time
import urllib.error
import urllib.request

H = 'http://127.0.0.1:8790'
LIB = 'http://127.0.0.1:8848'
PLUGIN = 'plugin.dsh'
OK = True


def check(name, cond, extra=''):
    global OK
    OK = OK and bool(cond)
    print('  %s %s%s' % ('✅' if cond else '❌', name, (' | ' + str(extra))[:150] if extra else ''))


def req(url, method='GET', body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {'raw': raw[:200]}
    except Exception as e:
        return None, {'err': '%s: %s' % (type(e).__name__, e)}


def set_level(lv):
    return req(H + '/api/permissions/set', 'POST', {'plugin': PLUGIN, 'level': lv})


def level():
    st, d = req(H + '/api/permissions?plugin=' + PLUGIN)
    return d.get('level'), d.get('released'), d.get('total')


def files():
    st, d = req(LIB + '/api/health')
    return d['stats']['files'] if d and d.get('ok') else -1


def notes_log_lines():
    try:
        with open(r'D:\core-notes\data\logs\notes.out.log', 'r', encoding='utf-8', errors='replace') as f:
            return sum(1 for _ in f)
    except Exception:
        return -1


orig = level()[0]
print('起始档位：', orig, '| 库文件数：', files())
try:
    # ① 权限连点 + 并发
    print('① 权限：串行连点 60 次 + 并发 10 路')
    bad = 0
    for i in range(60):
        lv = ['read', 'write', 'trust'][i % 3]
        st, d = set_level(lv)
        if st != 200 or d.get('ok') is not True:
            bad += 1
    check('串行 60 次全部成功', bad == 0, '失败 %d 次' % bad)
    errs = []
    cvs = []

    def one(lv):
        st, d = set_level(lv)
        cvs.append((st, d.get('ok'), d.get('level')))

    ts = [threading.Thread(target=one, args=(['read', 'write', 'trust'][i % 3],)) for i in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check('并发 10 路都返回 200 且 ok', all(c[0] == 200 and c[1] is True for c in cvs), cvs[:3])
    raw = open(r'D:\core\data\plugins\permissions.json', encoding='utf-8').read()
    try:
        obj = json.loads(raw)
        valid = isinstance(obj, dict) and obj.get(PLUGIN) in ('read', 'write', 'trust')
    except Exception as e:
        valid = False
    check('落盘仍是合法 JSON 且档位有效', valid, raw.strip()[:60])
    lv, rel, tot = level()
    check('读取的档位自洽（释放数对得上）', rel == {'read': 14, 'write': 22, 'trust': 31}.get(lv), '%s %s/%s' % (lv, rel, tot))

    # ② ops/call 并发（只读）
    print('② ops/call 并发 30 路（只读操作）')
    set_level('trust')
    res = []

    def call_op(op, args):
        st, d = req(H + '/api/ops/call', 'POST', {'plugin': PLUGIN, 'op': op, 'args': args})
        res.append((op, st, d.get('ok'), d.get('status')))

    ops = [('brain.health', {}), ('brain.search', {'q': '契约', 'limit': 1}),
           ('brain.tree', {}), ('brain.tags', {})]
    ts = [threading.Thread(target=call_op, args=ops[i % len(ops)]) for i in range(30)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check('30 路并发全成功', all(r[1] == 200 and r[2] is True for r in res),
          '失败：%s' % [r for r in res if not (r[1] == 200 and r[2] is True)][:2])

    # ③ 档位在变 + 并发 ops
    print('③ 一边狂切档位、一边并发调 ops（10 秒）')
    stop = False
    errs3 = []

    def flip():
        i = 0
        while not stop:
            set_level(['read', 'write', 'trust'][i % 3])
            i += 1

    def call3():
        while not stop:
            st, d = req(H + '/api/ops/call', 'POST',
                         {'plugin': PLUGIN, 'op': 'brain.update', 'args': {'id': 'no-such-id', 'content': 'x'}})
            # 允许：拒绝(档位不够) / 库404 / 成功都不是错；**500 或 非法响应**才是错
            if st not in (200,) or (d.get('ok') is None and d.get('denied') is None):
                errs3.append((st, str(d)[:80]))

    f1 = threading.Thread(target=flip)
    cs = [threading.Thread(target=call3) for _ in range(4)]
    f1.start()
    [c.start() for c in cs]
    time.sleep(10)
    stop = True
    f1.join()
    [c.join() for c in cs]
    check('切换档位期间并发调用没有 500 / 非法响应', not errs3, errs3[:2])
    check('库文件数没被这些调用改动', files() == 1162, files())

    # ④ 状态/日志并发 + 高频（验证缓存挡住库）
    print('④ 状态/日志接口：20 路并发 + 300 次高频询问')
    set_level(orig)
    got = []

    def getter(path):
        st, d = req(H + path)
        got.append((path, st))

    ts = [threading.Thread(target=getter, args=(p,)) for p in
          ['/api/host/status'] * 20 + ['/api/host/logsources'] * 5 + ['/api/host/log?source=core&tail=200'] * 10]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check('35 路并发全 200', all(g[1] == 200 for g in got), [g for g in got if g[1] != 200][:2])

    before = notes_log_lines()
    t0 = time.time()
    fails = 0
    for i in range(300):
        st, d = req(H + '/api/host/status')
        if st != 200:
            fails += 1
        time.sleep(0.1)
    el = time.time() - t0
    delta = notes_log_lines() - before
    check('%d 秒里 300 次状态询问全成功' % round(el), fails == 0, '失败 %d' % fails)
    check('库的访问日志增量很小（5 秒缓存挡住了高频）', delta <= 15, '涨了 %d 行' % delta)

    # ⑤ 并发建/删临时笔记
    print('⑤ 并发建 8 篇临时笔记 → 校验 → 并发删掉')
    made = []
    lock = threading.Lock()

    def create(i):
        st, d = req(H + '/api/ops/call', 'POST',
                     {'plugin': PLUGIN, 'op': 'brain.create',
                      'args': {'name': 'zz-并发压测-%d.md' % i, 'content': '# 压测 %d\n\n内容 %d' % (i, i)}})
        with lock:
            # 库的 create 返回的是 file_id（不是 id）—— 2026-09-21 我在这上面栽过，记在 §60
            made.append((i, st, (d.get('result') or {}).get('file_id'), d.get('ok')))

    ts = [threading.Thread(target=create, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    okc = [m for m in made if m[3] and m[2]]
    check('8 篇并发创建都成功', len(okc) == 8, made[:2])
    check('库文件数 = 1162 + 8', files() == 1170, files())

    def drop(i, fid):
        req(H + '/api/ops/call', 'POST', {'plugin': PLUGIN, 'op': 'brain.delete', 'args': {'id': fid}})

    ts = [threading.Thread(target=drop, args=(i, fid)) for (i, st, fid, ok) in okc]
    [t.start() for t in ts]
    [t.join() for t in ts]
    time.sleep(1)
    check('并发删除后回到 1162 篇', files() == 1162, files())
finally:
    set_level(orig)
    print('收尾：档位还原为', level()[0], '| 文件数', files())
print('=' * 60)
print('A 组结论：', '全部通过' if OK else '有失败')
