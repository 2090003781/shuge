#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""在 Win11 客人里开一个 PowerShell 窗口，敲一行命令，然后把屏幕截回来。

为什么走键盘：这台虚拟机的 SSH 通道现在是断的（客人里 sshd 没在听），
MCP 的 vm_exec/vm_put 都靠它，所以先用 QMP 的键鼠把诊断跑起来 —— 不依赖任何网络。

用法（在 WSL 里跑）：
  python3 vm-type.py "<要敲的 PowerShell 命令>" [--wait 25] [--shot /mnt/e/VMs/Win11/logs/typed.ppm]
"""
import argparse
import json
import os
import socket
import sys
import time

HOST = os.environ.get("QMP_HOST", "127.0.0.1")
PORT = int(os.environ.get("QMP_PORT", "4444"))

MAP = {
    'a': 'a', 'b': 'b', 'c': 'c', 'd': 'd', 'e': 'e', 'f': 'f', 'g': 'g', 'h': 'h', 'i': 'i',
    'j': 'j', 'k': 'k', 'l': 'l', 'm': 'm', 'n': 'n', 'o': 'o', 'p': 'p', 'q': 'q', 'r': 'r',
    's': 's', 't': 't', 'u': 'u', 'v': 'v', 'w': 'w', 'x': 'x', 'y': 'y', 'z': 'z',
    '0': '0', '1': '1', '2': '2', '3': '3', '4': '4', '5': '5', '6': '6', '7': '7', '8': '8', '9': '9',
    ' ': 'spc', '.': 'dot', ',': 'comma', '\\': 'backslash', '/': 'slash', '|': 'shift-backslash',
    ':': 'shift-semicolon', ';': 'semicolon', '>': 'shift-dot', '<': 'shift-comma',
    '-': 'minus', '=': 'equal', '_': 'shift-minus', '+': 'shift-equal', '(': 'shift-9', ')': 'shift-0',
    '!': 'shift-1', '@': 'shift-2', '#': 'shift-3', '$': 'shift-4', '%': 'shift-5', '^': 'shift-6',
    '&': 'shift-7', '*': 'shift-8', "'": 'apostrophe', '"': 'shift-apostrophe',
    '[': 'bracket_left', ']': 'bracket_right', '{': 'shift-bracket_left', '}': 'shift-bracket_right',
}


def connect():
    for _ in range(40):
        try:
            s = socket.create_connection((HOST, PORT), timeout=5)
            f = s.makefile("rwb")
            f.readline()
            f.write((json.dumps({"execute": "qmp_capabilities"}) + "\n").encode())
            f.flush()
            while True:
                line = f.readline()
                if not line:
                    break
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                if "return" in m or "error" in m:
                    break
            return s, f
        except Exception:
            time.sleep(0.5)
    raise SystemExit("无法连接 QMP %s:%d" % (HOST, PORT))


def cmd(f, obj):
    f.write((json.dumps(obj) + "\n").encode())
    f.flush()
    while True:
        line = f.readline()
        if not line:
            return None
        try:
            m = json.loads(line)
        except Exception:
            continue
        if "return" in m or "error" in m:
            return m


def key(f, codes, hold=60):
    ks = [{"type": "qcode", "data": c} for c in codes.split("-")]
    return cmd(f, {"execute": "send-key", "arguments": {"keys": ks, "hold-time": hold}})


def type_text(f, text, delay=0.035):
    skipped = []
    for ch in text:
        code = MAP.get(ch) or MAP.get(ch.lower())
        if code is None:
            skipped.append(ch)
            continue
        key(f, code)
        time.sleep(delay)
    return skipped


def shot(f, path):
    cmd(f, {"execute": "screendump", "arguments": {"filename": path}})
    time.sleep(1.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--wait", type=float, default=20.0, help="敲完之后等多久再截图")
    ap.add_argument("--shot", default="/mnt/e/VMs/Win11/logs/typed.ppm")
    ap.add_argument("--no-run", action="store_true", help="不按 Win+R，直接在已聚焦的窗口里敲")
    a = ap.parse_args()

    s, f = connect()
    try:
        if not a.no_run:
            print("Win+R …")
            key(f, "meta_l-r")
            time.sleep(2.0)
            # 直接在"运行"框里敲 powershell 命令（-NoExit 让窗口留着看结果）
            line = 'powershell -NoExit -Command "%s"' % a.text
            skipped = type_text(f, line)
            if skipped:
                print("跳过的字符（敲不出来）:", "".join(skipped))
            time.sleep(0.6)
            key(f, "ret")
        else:
            type_text(f, a.text)
            key(f, "ret")
        print("等 %.0f 秒看结果…" % a.wait)
        time.sleep(a.wait)
        shot(f, a.shot)
        print("截图:", a.shot)
    finally:
        try:
            s.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
