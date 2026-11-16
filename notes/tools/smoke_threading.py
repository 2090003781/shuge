# -*- coding: utf-8 -*-
"""并发验收：**读并发、写串行**（辞玖 2026-10-01「轻小请求先做」）。

  python tools/smoke_threading.py

对着**临时库**跑（真库完全不碰）：并发这事要造几十篇夹具、要真的转换导出，
往主人的库里塞不合适。向量工人关掉（AI_NOTES_VEC_WORKER=0 + VEC_SYNC_CAP=0），
免得它一边补向量一边跟测量抢 CPU。

验的是这次改造该有的样子：
  ① 8 个读同时打 → 都 200，且总墙钟远小于"排队做 8 次"
  ② 6 写 + 6 读同时打 → 写都 201、内容各是各的、**写入者标签不串味**
     （读也会认领写入者；标签若还是全局的，写的事件就会被读的标签盖掉 —— 这是并发最容易坏的地方）
  ③ 一个慢写（转换导出）在飞的时候，轻小请求（health / tree）**照样回得来**
     ← 这次改造的**唯一目的**。改前是单线程：轻小请求排队等慢写，实测等过 3506ms。
  ④ 慢写在飞的时候，第二个写**要等**（写与写仍然串行，跟改前一样安全）
  ⑤ 6 个检索同时打 → 都 200、同一个查询回同一批结果（共享缓存没被并发搅坏）
  ⑥ 全程没有 5xx、没有 database is locked；风暴过后写还是通的（写锁没漏在手里）
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OK, BAD, SKIP = [], [], []
SEEN = []                                     # 所有回复原文（收尾统一查 5xx / 锁冲突）
SEEN_LOCK = threading.Lock()


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print("  %s %s%s" % ("✅" if cond else "❌", name, ("  | " + str(extra)[:170]) if extra else ""))


def skip(name, why=""):
    SKIP.append(name)
    print("  ⏭ %s%s" % (name, ("  | " + str(why)[:170]) if why else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def req(base, path, body=None, method=None, timeout=600, writer=None):
    """回 (状态码, 解析后的对象, 秒数)。**不抛异常**：连接层失败回状态 0。"""
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    if writer:
        h["X-Writer"] = writer
    r = urllib.request.Request(base + path, data=data, method=method, headers=h)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw, st = resp.read().decode("utf-8", "replace"), resp.status
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        st = e.code
    except Exception as e:
        raw, st = "连接层失败：%s: %s" % (type(e).__name__, e), 0
    dt = time.perf_counter() - t0
    with SEEN_LOCK:
        SEEN.append((st, raw[:4000]))
    try:
        return st, json.loads(raw or "{}"), dt
    except Exception:
        return st, {"_raw": raw[:300]}, dt


def fan(fns):
    """把一组零参函数**同时**放出去，按传入顺序回结果（每个都是 req 的三元组）。"""
    out = [None] * len(fns)

    def run(i, f):
        try:
            out[i] = f()
        except Exception as e:                       # 兜底：一个挂了不许把整轮拖垮
            out[i] = (0, {"_exc": "%s: %s" % (type(e).__name__, e)}, 0.0)
    ts = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(fns)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


def main() -> int:
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    data = tempfile.mkdtemp(prefix="zz-thr-")
    env = dict(os.environ)
    env["AI_NOTES_VEC_WORKER"] = "0"        # 向量工人关掉：别跟测量抢 CPU
    env["AI_NOTES_VEC_SYNC_CAP"] = "0"      # 保存时的同步嵌入也关掉：让"写"的耗时只反映写本身
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "src", "server.py"),
                            "--port", str(port), "--data", data], cwd=ROOT, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        print("⓪ 起临时库（随机端口 + 临时 data）")
        t0 = time.time()
        up = False
        while time.time() - t0 < 150:
            if req(base, "/api/health", timeout=5)[0] == 200:
                up = True
                break
            time.sleep(0.5)
        chk("临时库起得来", up, "port=%d data=%s 用时 %.1fs" % (port, os.path.basename(data), time.time() - t0))
        if not up:
            return 1

        print("\n① 造夹具：24 篇小笔记（顺序建，不算并发）")
        fids, t0 = [], time.perf_counter()
        for i in range(24):
            body = "# zz-并发夹具 %02d\n\n" % i + "\n\n".join(
                "## 第 %d 节\n\n%s" % (j, ("并发夹具正文 %02d-%d。" % (i, j)) * 16) for j in (1, 2, 3))
            st, d, _ = req(base, "/api/files", {"name": "zz-并发夹具-%02d.md" % i, "content": body})
            if st == 201 and d.get("file_id"):
                fids.append(d["file_id"])
        chk("建了 24 篇夹具", len(fids) == 24, "%.1fs" % (time.perf_counter() - t0))

        print("\n② 8 个读同时打（health / tree / lock / folders / tags / properties）")
        _, _, h1 = req(base, "/api/health", timeout=60)
        paths = ["/api/health", "/api/tree", "/api/lock", "/api/folders",
                 "/api/tags", "/api/health", "/api/tree", "/api/properties"]
        t0 = time.perf_counter()
        rs = fan([(lambda p=p: req(base, p, timeout=120)) for p in paths])
        wall = time.perf_counter() - t0
        sts = [r[0] for r in rs]
        chk("8 个读都 200", all(s == 200 for s in sts), sts)
        slowest = max(r[2] for r in rs)
        if h1 >= 0.03:
            chk("8 个读的总墙钟 < 单次×8（说明是真并发，不是排队）",
                wall < h1 * 8, "单次 %.0fms，8 个并发总墙钟 %.0fms" % (h1 * 1000, wall * 1000))
        else:
            skip("并发 vs 排队对比", "单次太快（%.1fms），比不出来" % (h1 * 1000))
        chk("8 个读里最慢的一个也 < 2s", slowest < 2.0, "%.0fms" % (slowest * 1000))

        print("\n③ 6 个写 + 6 个读同时打（写入者标签最容易在这儿串味）")
        def mk_write(i):
            def f():
                return req(base, "/api/files",
                           {"name": "zz-并发写-%d.md" % i,
                            "content": "# zz-并发写 %d\n\n这一篇的独门记号是 MARK%d，别人不该有。\n" % (i, i)},
                           writer="tool:thr%d" % i, timeout=300)
            return f
        mixed = [mk_write(i) for i in range(6)] + \
                [(lambda: req(base, "/api/health", timeout=120)) for _ in range(6)]
        rs = fan(mixed)
        wr = rs[:6]
        chk("6 个写都 201", all(r[0] == 201 for r in wr), [r[0] for r in wr])
        new_ids = [r[1].get("file_id") for r in wr]
        chk("6 个 id 互不相同", all(new_ids) and len(set(new_ids)) == 6, new_ids)

        wrong_body, wrong_tag = [], []
        for i, fid in enumerate(new_ids):
            _, d, _ = req(base, "/api/files/%s" % fid)
            if ("MARK%d" % i) not in json.dumps(d, ensure_ascii=False):
                wrong_body.append(i)
            _, w, _ = req(base, "/api/files/%s/writers" % fid)
            tags = {x.get("writer") for x in (w.get("writers") or [])}
            if ("tool:thr%d" % i) not in tags:
                wrong_tag.append((i, sorted(tags)))
        chk("每篇正文都是自己那份（没串）", not wrong_body, wrong_body)
        chk("6 个写入者标签各归各位（读的标签没盖掉写的）", not wrong_tag, wrong_tag[:3])
        chk("6 个读也都 200", all(r[0] == 200 for r in rs[6:]), [r[0] for r in rs[6:]])

        print("\n④ 一个慢写在飞时，轻小请求照样回（这次改造的唯一目的）")
        long_body = "# zz-慢写夹具\n\n" + "\n\n".join(
            "## 第 %d 节\n\n%s" % (i, "撑页面的正文。" * 44) for i in range(1, 9))
        st, d, _ = req(base, "/api/files", {"name": "zz-慢写夹具.md", "content": long_body})
        slow_id = d.get("file_id")
        chk("造出慢写夹具（长文）", st == 201 and bool(slow_id), st)

        marks, reads = {}, []
        def slow_pdf():
            marks["w1"] = req(base, "/api/files/%s/convert" % slow_id, {"to": "pdf"}, timeout=600)
            marks["w1_end"] = time.perf_counter()
        marks["w1_start"] = time.perf_counter()       # 计时起点在主线程打，别指望子线程先写
        th = threading.Thread(target=slow_pdf)
        th.start()
        time.sleep(0.35)                              # 让它先进去（真在写库）
        while th.is_alive() and time.perf_counter() - marks["w1_start"] < 300:
            reads.append(req(base, "/api/health", timeout=60))
            reads.append(req(base, "/api/tree", timeout=60))
        th.join()
        slow_dt = marks.get("w1_end", 0) - marks.get("w1_start", 0)
        print("     （慢写 = 转 PDF，用了 %.2fs；期间夹了 %d 个轻小请求）" % (slow_dt, len(reads)))
        if slow_dt >= 1.0:
            good = [r for r in reads if r[0] == 200]
            worst = max([r[2] for r in reads] or [0])
            chk("慢写在飞时，轻小请求回了 ≥3 次", len(good) >= 3,
                "%d/%d 次成功，最慢 %.0fms" % (len(good), len(reads), worst * 1000))
            chk("慢写在飞时，单个轻小请求也 < 1.5s", worst < 1.5, "%.0fms" % (worst * 1000))
        else:
            skip("「慢写不堵小请求」两条", "慢写只用了 %.2fs（WPS 不可用？），窗口太短看不出" % slow_dt)

        print("\n⑤ 慢写在飞时，第二个写要等（写与写仍然串行）")
        if slow_dt >= 1.0:
            m2 = {"w1_start": time.perf_counter()}
            def slow2():
                m2["w1"] = req(base, "/api/files/%s/convert" % slow_id, {"to": "pdf"}, timeout=600)
                m2["w1_end"] = time.perf_counter()
            th2 = threading.Thread(target=slow2)
            th2.start()
            time.sleep(0.35)
            st2, d2, _ = req(base, "/api/files", {"name": "zz-串行-2.md", "content": "# 第二个写\n"})
            m2["w2_end"] = time.perf_counter()
            th2.join()
            gap = m2["w2_end"] - m2.get("w1_end", 0)
            chk("第二个写等到了第一个写干完（串行没破）", gap >= -0.05,
                "第二个写在第一个写结束后 %.0fms 才回；第一个写用时 %.2fs"
                % (gap * 1000, m2.get("w1_end", 0) - m2.get("w1_start", 0)))
            chk("第二个写本身也成功了", st2 == 201, [st2, d2.get("error")])
        else:
            skip("「写与写仍然串行」", "上一段的慢写不够慢，拿不到窗口")

        print("\n⑥ 6 个检索同时打（共享缓存别被并发搅坏）")
        q = "/api/search?q=%s&mode=kw&limit=20" % urllib.parse.quote("并发夹具正文")
        rs = fan([(lambda: req(base, q, timeout=180)) for _ in range(6)])
        chk("6 个关键词检索都 200", all(r[0] == 200 for r in rs), [r[0] for r in rs])
        sig = []
        for r in rs:
            ids = tuple(h.get("file_id") for h in (r[1].get("hits") or []))
            sig.append(ids)
        chk("同一个查询并发 6 次，回的是同一批（顺序也一样）", len(set(sig)) == 1,
            ["%d 条" % len(s) for s in sig])
        chk("这批检索确实有命中（不是空跑）", bool(sig[0]), len(sig[0]))

        qh = "/api/search?q=%s&mode=hybrid&rerank=0&limit=10" % urllib.parse.quote("并发夹具正文")
        rh = fan([(lambda: req(base, qh, timeout=600)) for _ in range(2)])
        chk("2 个混合检索都 200（含向量/精排那条链）", all(r[0] == 200 for r in rh),
            [(r[0], r[2]) for r in rh])
        if all(r[0] == 200 for r in rh):
            sig2 = [tuple(h.get("file_id") for h in (r[1].get("hits") or [])) for r in rh]
            chk("同一个混合查询并发 2 次，结果一致", sig2[0] == sig2[1], ["%d 条" % len(s) for s in sig2])

        print("\n⑦ 收尾：全程没 5xx / 没锁冲突；风暴过后写还是通的")
        five = [s for s in SEEN if s[0] >= 500]
        chk("全程没有 5xx", not five, five[:2])
        conflict = [s for s in SEEN if "database is locked" in s[1] or "database table is locked" in s[1]]
        chk("全程没有 database is locked", not conflict, conflict[:2])
        t0 = time.perf_counter()
        st, d, _ = req(base, "/api/files", {"name": "zz-收尾.md", "content": "# 收尾\n"}, timeout=30)
        took = time.perf_counter() - t0
        chk("风暴过后写还是通的（写锁没漏在手里）", st == 201, [st, d.get("error")])
        chk("而且不慢（<10s，真漏锁会一直挂）", took < 10, "%.2fs" % took)
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            srv.kill()
        shutil.rmtree(data, ignore_errors=True)
        print("\n临时库已杀、目录已删（真库未被触碰）")

    print("\n通过 %d / %d；跳过 %d；失败：%s"
          % (len(OK), len(OK) + len(BAD), len(SKIP), "、".join(BAD) if BAD else "无"))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
