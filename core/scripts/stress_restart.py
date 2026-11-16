"""并发/冲突 B 组（有启停动作，注意安全）：
   ① 库：连续 5 次 restart（间隔 1s）→ 实例数必须恒为 1
   ② 库：并发 3 个 restart → 实例数必须恒为 1
   ③ 库：kill 之后立刻 restart（退出看护 vs 人工重启的竞态）
   ④ 内核：连续 3 次 restart → 实例数 1、账本 seq 单调
   ⑤ 内核：并发 2 个 restart
   全程监视：python src/server.py 的实例数、core.exe 实例数、账本 seq 是否倒退
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
import subprocess
import threading
import time
import urllib.error
import urllib.request

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

H = 'http://127.0.0.1:8790'
LIB = 'http://127.0.0.1:8848'
OK = True


def check(name, cond, extra=''):
    global OK
    OK = OK and bool(cond)
    print('  %s %s%s' % ('✅' if cond else '❌', name, (' | ' + str(extra))[:150] if extra else ''))


def req(url, method='GET', body=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {'raw': e.read().decode()[:120]}
    except Exception as e:
        return None, {'err': '%s: %s' % (type(e).__name__, e)}


def procs(image):
    out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq ' + image, '/FO', 'CSV', '/NH'],
                         capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    return [c[1] for c in ([x.strip('"') for x in l.split('","')] for l in out.splitlines())
            if len(c) >= 2 and c[0] == image]


def lib_instances():
    """监听 8848 的进程数（真库实例）"""
    out = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    pids = set()
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 5 and f[0] == 'TCP' and f[1].endswith(':8848') and f[3] == 'LISTENING':
            pids.add(f[4])
    return pids


def lib_ok():
    st, d = req(LIB + '/api/health')
    return bool(d and d.get('ok'))


def wait_lib(t=90):
    t0 = time.time()
    while time.time() - t0 < t:
        if lib_ok():
            return time.time() - t0
        time.sleep(0.2)
    return None


def core_ok():
    st, d = req(H + '/api/core/status')
    return bool(d and d.get('running'))


def wait_core(t=90):
    t0 = time.time()
    while time.time() - t0 < t:
        if core_ok():
            return time.time() - t0
        time.sleep(0.2)
    return None


def seq():
    st, d = req(H + '/api/core/status')
    return ((d or {}).get('ledger') or {}).get('last_seq') or (d or {}).get('seq')


def files():
    st, d = req(LIB + '/api/health')
    return (d or {}).get('stats', {}).get('files')


max_lib = 0
max_core = 0
seqs = []
stop = False


def watch():
    global max_lib, max_core
    while not stop:
        max_lib = max(max_lib, len(lib_instances()))
        max_core = max(max_core, len(procs('core.exe')))
        s = seq()
        if s:
            seqs.append(s)
        time.sleep(0.25)


w = threading.Thread(target=watch, daemon=True)
w.start()
try:
    print('① 库连续 5 次 restart（每次间隔 1 秒）')
    for i in range(5):
        st, d = req(H + '/api/notes/restart', 'POST', {})
        t = wait_lib()
        print('   第 %d 次：restart=%s 恢复 %.2f 秒 实例数 %d' % (i + 1, (d or {}).get('ok'), t or -1, len(lib_instances())))
        time.sleep(1)
    check('库实例数始终为 1', max_lib == 1, '观察到的峰值 %d' % max_lib)
    check('库仍然健康且篇数不变', lib_ok() and files() == 1162, files())

    print('② 并发 3 个库 restart')
    res = []

    def r3():
        res.append(req(H + '/api/notes/restart', 'POST', {}))

    ts = [threading.Thread(target=r3) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    t = wait_lib()
    check('并发 restart 后库健康（%.1f 秒）' % (t or -1), t is not None)
    time.sleep(2)
    check('并发 restart 后实例数仍为 1', len(lib_instances()) == 1, len(lib_instances()))
    print('   三个请求的返回：', [str(r[1])[:60] for r in res])

    print('③ kill 库之后**立刻** restart（看护 vs 人工 竞态）')
    pids = list(lib_instances())
    for p in pids:
        subprocess.run(['taskkill', '/PID', p, '/F'], capture_output=True, text=True, creationflags=NO_WINDOW)
    req(H + '/api/notes/restart', 'POST', {})
    t = wait_lib()
    time.sleep(2.5)
    check('竞态后恢复（%.2f 秒）' % (t or -1), t is not None)
    check('竞态后实例数仍为 1', len(lib_instances()) == 1, len(lib_instances()))

    print('④ 内核连续 3 次 restart')
    for i in range(3):
        st, d = req(H + '/api/core/restart', 'POST', {})
        t = wait_core()
        print('   第 %d 次：restart=%s 恢复 %.2f 秒 core 实例 %d seq=%s'
              % (i + 1, (d or {}).get('ok'), t or -1, len(procs('core.exe')), seq()))
        time.sleep(1)
    check('内核实例数始终为 1', max_core == 1, '观察到的峰值 %d' % max_core)
    check('账本 seq 单调不倒退', all(seqs[i] <= seqs[i + 1] for i in range(len(seqs) - 1)),
          '样本 %d 个：%s…%s' % (len(seqs), seqs[0] if seqs else None, seqs[-1] if seqs else None))

    print('⑤ 并发 2 个内核 restart')
    ts = [threading.Thread(target=lambda: req(H + '/api/core/restart', 'POST', {})) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    t = wait_core()
    time.sleep(2)
    check('并发 restart 后内核健康（%.1f 秒）' % (t or -1), t is not None)
    check('并发 restart 后 core 实例数为 1', len(procs('core.exe')) == 1, len(procs('core.exe')))
finally:
    stop = True
    w.join(timeout=3)

print('⑥ 收尾状态')
st, d = req(H + '/api/host/status')
print('   host_paused=%s notes.healthy=%s core.online=%s' %
      (d.get('host_paused'), d['notes']['healthy'], d['core']['online']))
print('   库实例 %d 个，core 实例 %d 个，篇数 %s' % (len(lib_instances()), len(procs('core.exe')), files()))
print('=' * 60)
print('B 组结论：', '全部通过' if OK else '有失败')
