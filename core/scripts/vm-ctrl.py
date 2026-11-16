#!/usr/bin/env python3
"""给客人发一次 Ctrl+数字（切壳里的页签用），然后截图。

用法: python3 vm-ctrl.py 4 [截图名]
"""
import json
import os
import socket
import subprocess
import sys
import time

HOST = os.environ.get("QMP_HOST", "127.0.0.1")
PORT = int(os.environ.get("QMP_PORT", "4444"))
LOGS = "/mnt/e/VMs/Win11/logs"

DIGIT = {"1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6", "7": "7", "8": "8", "9": "9"}


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
    raise SystemExit("无法连接 QMP")


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


def key(f, codes, hold=80):
    ks = [{"type": "qcode", "data": c} for c in codes.split("-")]
    return cmd(f, {"execute": "send-key", "arguments": {"keys": ks, "hold-time": hold}})


def main():
    digit = sys.argv[1] if len(sys.argv) > 1 else "4"
    name = sys.argv[2] if len(sys.argv) > 2 else "ctrl"
    s, f = connect()
    try:
        # Ctrl 按住 → 数字 → 松开：用 ctrl-<digit> 组合键
        key(f, "ctrl-%s" % DIGIT.get(digit, "4"))
        time.sleep(1.2)
        # 多按几次，确保窗口拿到焦点
        key(f, "ctrl-%s" % DIGIT.get(digit, "4"))
        time.sleep(3.0)
        ppm = os.path.join(LOGS, name + ".ppm")
        png = os.path.join(LOGS, name + ".png")
        cmd(f, {"execute": "screendump", "arguments": {"filename": ppm}})
        time.sleep(1.5)
    finally:
        s.close()
    subprocess.run(["python3", "/mnt/e/VMs/_tools/ppm2png_fast.py", ppm, png], capture_output=True, text=True)
    print(png)


if __name__ == "__main__":
    main()
