#!/usr/bin/env python3
"""在客人里点一下（QMP 绝对坐标，usb-tablet），然后截图。

用法: python3 vm-click.py <x> <y> [截图名]
屏幕 1280x800；绝对坐标按 0..32767 折算。
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
SCR_W, SCR_H = 1280, 800


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


def move(f, x, y):
    ax = int(round(x / SCR_W * 32767))
    ay = int(round(y / SCR_H * 32767))
    return cmd(f, {"execute": "input-send-event", "arguments": {"events": [
        {"type": "abs", "data": {"axis": "x", "value": ax}},
        {"type": "abs", "data": {"axis": "y", "value": ay}},
    ]}})


def click(f):
    cmd(f, {"execute": "input-send-event", "arguments": {"events": [
        {"type": "btn", "data": {"down": True, "button": "left"}}]}})
    time.sleep(0.12)
    cmd(f, {"execute": "input-send-event", "arguments": {"events": [
        {"type": "btn", "data": {"down": False, "button": "left"}}]}})


def main():
    x = int(sys.argv[1]) if len(sys.argv) > 1 else 58
    y = int(sys.argv[2]) if len(sys.argv) > 2 else 177
    name = sys.argv[3] if len(sys.argv) > 3 else "click"
    s, f = connect()
    try:
        move(f, x, y)
        time.sleep(0.4)
        click(f)                 # 先点一下把窗口带到前台
        time.sleep(0.8)
        move(f, x, y)
        time.sleep(0.3)
        click(f)                 # 再点一次确保命中
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
