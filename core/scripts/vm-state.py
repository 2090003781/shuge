#!/usr/bin/env python3
"""看这台 Win11 虚拟机现在什么状态（QEMU 在不在、QMP 说什么、屏幕什么样）。"""
import json
import os
import socket
import subprocess
import time

LOGS = "/mnt/e/VMs/Win11/logs"


def qmp(commands):
    s = socket.create_connection(("127.0.0.1", 4444), timeout=6)
    f = s.makefile("rwb")
    f.readline()
    out = []
    for c in commands:
        f.write((json.dumps(c) + "\n").encode())
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
                out.append(m)
                break
    s.close()
    return out


def main():
    procs = subprocess.run(["pgrep", "-af", "qemu-system"], capture_output=True, text=True).stdout.strip()
    print("QEMU 进程：", procs.splitlines()[0][:120] if procs else "（没有）")
    try:
        r = qmp([{"execute": "qmp_capabilities"}, {"execute": "query-status"}])
        print("QMP status：", r[-1] if r else "（没答话）")
    except Exception as e:
        print("QMP 连不上：", e)
        return
    # 顺带截一张
    ppm = os.path.join(LOGS, "state.ppm")
    qmp([{"execute": "screendump", "arguments": {"filename": ppm}}])
    time.sleep(1.2)
    subprocess.run(["python3", "/mnt/e/VMs/_tools/ppm2png_fast.py", ppm, os.path.join(LOGS, "state.png")],
                   capture_output=True, text=True)
    print("截图：", os.path.join(LOGS, "state.png"))


if __name__ == "__main__":
    main()
