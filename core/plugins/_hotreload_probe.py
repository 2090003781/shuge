#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**测试用**假插件：只为 `scripts/smoke_hotreload.py` 服务（平时不挂进 config.yaml）。

它做两件事，别的什么都不做：
  ① 启动时向内核注册自己（走 plugins/lib/probe.py 的 http_post，带插件面 token）；
  ② 起一个 Probe 应答器（能被内核点名）。
另外把一个「我活着」的文件写到 $HOTRELOAD_TAG，自测据此确认它真起来了（而不是内核记账记上了）。
"""
import argparse
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))

from probe import Probe, http_post        # noqa: E402

DEFAULT_ID = "plugin.hotreloadtest"
_stop = False


def _bye(_sig=None, _frm=None):
    global _stop
    _stop = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default=DEFAULT_ID)
    a = ap.parse_args()
    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)

    probe = Probe(a.id, handlers={"hotreload.ping": lambda args=None: {"ok": True, "pong": True},
                                  "hotreload.status": lambda args=None: {"ok": True, "up": True}},
                  stats_provider=lambda: dict(probe.stats()))
    probe.start()
    try:
        http_post("/api/bridge/plugins/register", {
            "id": a.id, "name": "热加载自测假插件",
            "description": "只在 scripts/smoke_hotreload.py 里被临时加进 config.yaml，用完就撤。",
            "capabilities": ["hotreload.ping", "hotreload.status"],
            "produces": [], "consumes": []})
    except Exception as e:                       # 注册失败也留着：自测会看到"没注册上"并报出来
        print("hotreload-probe: 注册失败 %s: %s" % (type(e).__name__, e), flush=True)

    # 自测据此确认"进程真活着"（而不是内核只是记了账）。
    # ⚠️ 不能用自测进程的环境变量：插件是内核 spawn 的，只拿得到 supervisor 给的那几个
    #    （PLUGIN_TOKEN/CORE_URL/CORE_DIR）—— 所以路径由 CORE_DIR 算出来，环境变量仅作可选覆盖。
    tag = os.environ.get("HOTRELOAD_TAG") or os.path.join(
        CORE_DIR, "data", "tmp", "hotreload-alive.txt")
    if tag:
        try:
            with open(tag, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass
    print("hotreload-probe: 已起 pid=%s id=%s" % (os.getpid(), a.id), flush=True)
    while not _stop:
        time.sleep(0.5)
    probe.stop()
    print("hotreload-probe: 退出", flush=True)


if __name__ == "__main__":
    main()
