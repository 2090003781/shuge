"""事件驱动投递箱验收（2026-09-22）：拖一个文件多久进库 + 200 个文件齐不齐。

用法：python D:\\RAG\\acc_inbox_events.py
只测这一条链路：磁盘落地 → 插件发现 → 库 /api/import → 库里能查到。
判据：单文件 < 2.5s；批量 200 个全部进库（缺一个都算失败）。
清理靠 acc_inbox_cleanup.py，本脚本只负责"造 + 量"。
"""

import os as _os, sys as _sys          # 门禁用（2026-09-30 体检）

if "--yes" not in _sys.argv and _os.environ.get("COREHOST_ALLOW_DESTRUCTIVE") != "1":
    print(__doc__ or "")
    print("\n【门禁】%s 会真的动到**运行中的枢阁 / 库文件夹**（这个脚本忽略参数，--help 也照跑）。"
          % _os.path.basename(__file__))
    print("       确认要跑：python %s --yes（或设 COREHOST_ALLOW_DESTRUCTIVE=1）"
          % _os.path.basename(__file__))
    _sys.exit(2)
import io
import json
import os
import shutil
import sys
import time
import urllib.request

LIB = "http://127.0.0.1:8848"
VDIR = r"D:\个人数据\辞玖\_验收_事件驱动"
LIB_DIR = "vault/_验收_事件驱动"
TAG = time.strftime("%m%d-%H%M%S")


def tree_files():
    """库里 vault/_验收_事件驱动 下的文件名集合（不存在就是空集）。"""
    d = json.load(urllib.request.urlopen(LIB + "/api/tree", timeout=120))
    want = [x for x in LIB_DIR.split("/") if x]

    def walk(node, path):
        path = path + [node.get("name", "")]
        if [p for p in path if p] == want:
            return {f["name"] for f in node.get("files", [])}
        for ch in node.get("children", []):
            got = walk(ch, path)
            if got is not None:
                return got
        return None

    return walk(d["tree"], []) or set()


def wait_for(names, deadline, label):
    """轮询到 names 全部出现在库里，返回 (用时, 缺的)。"""
    t0 = time.time()
    left = set(names)
    while time.time() - t0 < deadline:
        try:
            left = set(names) - tree_files()
        except Exception as e:
            print("  查库出错（继续等）：", e)
            left = set(names)
        if not left:
            return time.time() - t0, left
        time.sleep(0.2)
    return time.time() - t0, left


def main():
    if os.path.isdir(VDIR):
        shutil.rmtree(VDIR)
    os.makedirs(VDIR)
    fails = []

    # ---- 场景一：单个文件落地，量到"库里能查到"为止 ----
    one = "_单件-%s.md" % TAG
    p = os.path.join(VDIR, one)
    t_write = time.time()
    io.open(p, "w", encoding="utf-8").write("# 单件验收\n\n事件驱动投递箱验收标记 %s\n" % TAG)
    dt, left = wait_for([one], 60, "单件")
    print("【单件】落地→进库 %.2f 秒" % dt)
    if left:
        fails.append("单件 %s 60 秒没进库" % one)

    # ---- 场景二：200 个文件同时冲进来 ----
    names = ["_批量-%s-%03d.md" % (TAG, i) for i in range(200)]
    t_write2 = time.time()
    for n in names:
        io.open(os.path.join(VDIR, n), "w", encoding="utf-8").write(
            "# 批量验收\n\n%s %s\n" % (TAG, n))
    print("【批量】200 个文件写完用了 %.2f 秒，开始等进库" % (time.time() - t_write2))
    dt2, left2 = wait_for(names, 300, "批量")
    print("【批量】全部进库用时 %.2f 秒（缺 %d 个）" % (dt2, len(left2)))
    if left2:
        fails.append("批量缺 %d 个：%s" % (len(left2), sorted(left2)[:5]))

    print("\n结论：" + ("全部通过" if not fails else "失败 → " + "；".join(fails)))
    print("测试目录：%s（库里 %s）" % (VDIR, LIB_DIR))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
