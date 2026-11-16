# -*- coding: utf-8 -*-
"""弹系统「选文件 / 选文件夹」对话框，把结果写成 JSON —— 库自己不起 GUI，交给这个一次性进程。

为什么另起进程：库是**单线程**服务（一个请求一个请求地办），要是在处理请求的线程里弹模态窗口，
窗口没关之前整库都被堵住（搜索、保存全卡，探针还会以为库不健康）。所以库只负责"起这个进程 + 轮询结果"。

服务端用法：
  python tools/pick_path.py --kind file|dir --out <结果json> [--title 标题] [--timeout 300]

验收用法（不弹窗，纯协议）：
  python tools/pick_path.py --self-test --out <结果json> --fake "D:\\某个路径"

结果 JSON：{"state": "ok"|"canceled"|"error", "paths": [...], "error": "..."}
"""
import argparse
import json
import os
import sys
import time


def write_out(out: str, obj: dict) -> None:
    """原子写：库那边只认"文件已经写完整"的状态（先写 .tmp 再 replace）"""
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=("file", "dir"), default="file")
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--timeout", type=int, default=300, help="窗口最多开多久（秒），到点自动关")
    ap.add_argument("--self-test", action="store_true", help="不弹窗，直接写结果（验收用）")
    ap.add_argument("--fake", default="", help="配合 --self-test：假装选了这个路径")
    a = ap.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")

    if a.self_test:
        write_out(a.out, {"state": "ok" if a.fake else "canceled",
                          "paths": [a.fake] if a.fake else [],
                          "at": stamp, "self_test": True})
        return 0

    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:                     # 没装 tkinter / 没有图形环境
        write_out(a.out, {"state": "error", "paths": [], "at": stamp,
                          "error": "起不了选择框（%s: %s）" % (type(e).__name__, str(e)[:80])})
        return 2

    try:
        root = tk.Tk()
        root.withdraw()                        # 只要对话框，不要那个空的主窗口
        for attr, val in (("-topmost", True), ("-toolwindow", True)):
            try:
                root.attributes(attr, val)
            except Exception:
                pass
        title = a.title or ("选一个文件" if a.kind == "file" else "选一个文件夹")
        fired = {"timeout": False}

        def on_timeout():
            fired["timeout"] = True
            try:
                root.destroy()
            except Exception:
                pass

        timer = root.after(max(5, int(a.timeout)) * 1000, on_timeout)
        try:
            if a.kind == "file":
                picked = filedialog.askopenfilename(parent=root, title=title)
            else:
                picked = filedialog.askdirectory(parent=root, title=title, mustexist=True)
        finally:
            try:
                root.after_cancel(timer)
            except Exception:
                pass
            try:
                root.destroy()
            except Exception:
                pass
        if fired["timeout"]:
            write_out(a.out, {"state": "canceled", "paths": [], "at": stamp,
                              "why": "超时自动关掉了选择框"})
            return 0
        picked = (picked or "").strip()
        write_out(a.out, {"state": "ok" if picked else "canceled",
                          "paths": [picked.replace("/", "\\")] if picked else [], "at": stamp})
        return 0
    except Exception as e:
        write_out(a.out, {"state": "error", "paths": [], "at": stamp,
                          "error": "%s: %s" % (type(e).__name__, str(e)[:120])})
        return 2


if __name__ == "__main__":
    sys.exit(main())
