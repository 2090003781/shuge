#!/usr/bin/env python3
"""只截一张客人的图（QMP screendump → png）。用法: python3 vm-shot.py [输出名]"""
import json
import os
import socket
import subprocess
import sys
import time

HOST = os.environ.get("QMP_HOST", "127.0.0.1")
PORT = int(os.environ.get("QMP_PORT", "4444"))
LOGS = "/mnt/e/VMs/Win11/logs"


def connect():
    for _ in range(30):
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


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "shot"
    ppm = os.path.join(LOGS, name + ".ppm")
    png = os.path.join(LOGS, name + ".png")
    s, f = connect()
    try:
        f.write((json.dumps({"execute": "screendump", "arguments": {"filename": ppm}}) + "\n").encode())
        f.flush()
        time.sleep(1.5)
    finally:
        s.close()
    r = subprocess.run(["python3", "/mnt/e/VMs/_tools/ppm2png_fast.py", ppm, png],
                       capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    print(png)


if __name__ == "__main__":
    main()
