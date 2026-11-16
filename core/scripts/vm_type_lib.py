#!/usr/bin/env python3
"""QMP 键鼠小工具（给别的脚本 import 用）。"""
import json
import os
import socket
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
    '\n': 'ret', '\r': 'ret', '\t': 'tab',
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


def type_text(f, text, delay=0.04):
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
    time.sleep(1.2)
