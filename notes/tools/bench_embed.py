# -*- coding: utf-8 -*-
"""向量计算该不该"多线程"？—— 用数字回答（辞玖 2026-10-01：「像向量计算等等先规划好怎么分线程」）。

  python tools/bench_embed.py

量三件事（同一个 bge-m3、跟检索用的是同一个实例、同一台机器）：

  ① 8 条查询：逐条串行 vs 8 条并发（8 线程）vs 一次批量 encode —— 谁快？
  ② 32 个文档块：逐条 vs 一次批量 —— 批量到底值多少（这是入库那条路）
  ③ 单条延迟 —— 决定"查询该不该排队"

**为什么要有它**：`@分线程规划` 里"不给嵌入开多线程、要攒批"这条口径就是靠这组数字定的。
要让结论站得住，就得随时能重跑一遍（换设备 / 换模型 / 换机器之后尤其）。

读法：看 **相对**那一栏（绝对值会被机器上别的重活带偏）。
历史实测（16 逻辑核 / CPU / bge-m3）：并发 **1.38×**、批量 **1.60×**、入库批量 **1.77×**。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from embedder import get_embedder          # noqa: E402

QS = ["并发测量句子 %d：这是一段用来测嵌入吞吐的中文文本。" % i for i in range(8)]
DOCS = ["文档块 %d：枢阁的正文切片，长度大致与真实入库块相当。" % i for i in range(32)]


def best(e, fn, rounds=2):
    """跑 rounds 遍取最快（机器上常有别人的重活，取最快更接近真值）"""
    out = []
    for _ in range(rounds):
        e._qcache.clear()                  # 查询缓存会作弊，每遍清掉
        t = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t)
    return min(out)


def main() -> int:
    e = get_embedder()
    t0 = time.perf_counter()
    e.encode_one("预热")
    print("模型加载 %.1fs（device=%s）\n" % (time.perf_counter() - t0, e.device))

    def serial_q():
        for q in QS:
            e.encode([q])

    def concurrent_q():
        out = [None] * len(QS)

        def w(i):
            out[i] = e.encode_one(QS[i])
        ts = [threading.Thread(target=w, args=(i,)) for i in range(len(QS))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

    def serial_docs():
        for d in DOCS:
            e.encode([d])

    a = best(e, serial_q)
    b = best(e, concurrent_q)
    c = best(e, lambda: e.encode(QS))
    d = best(e, serial_docs)
    f = best(e, lambda: e.encode(DOCS, batch_size=32))
    g = best(e, lambda: e.encode([DOCS[0]]))

    print("① 8 条查询（同一个模型实例）")
    print("   逐条串行          %6.3fs   基准 1.00×" % a)
    print("   8 条并发(8线程)   %6.3fs   %.2f× 相对串行" % (b, a / b if b else 0))
    print("   一次批量 encode   %6.3fs   %.2f× 相对串行" % (c, a / c if c else 0))
    print("\n② 32 个文档块（入库那条路）")
    print("   逐条 32 次        %6.3fs   基准 1.00×" % d)
    print("   一次批量          %6.3fs   %.2f× 相对逐条" % (f, d / f if f else 0))
    print("\n③ 单个文档块延迟    %6.3fs" % g)
    print("\n口径：**并发那一栏 < 批量那一栏** → 该攒批，不该加线程（16 核 CPU 上就是这个结论）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
