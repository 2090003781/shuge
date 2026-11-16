#!/usr/bin/env python3
"""解锁客人的锁屏：回车关掉提示 → 敲密码 → 回车 → 截图看结果。

密码默认取 123456（主人 WSL 里 rdp-drive.sh 用的就是这个）。
用法: python3 vm-unlock2.py [密码]
"""
import json
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, "/mnt/e/VMs/_tools")
from vm_type_lib import connect, key, type_text, shot  # type: ignore


def main():
    pw = sys.argv[1] if len(sys.argv) > 1 else "123456"
    s, f = connect()
    try:
        print("唤一下屏幕（按个键）…")
        key(f, "spc")
        time.sleep(1.5)
        print("回车关掉可能的提示…")
        key(f, "ret")
        time.sleep(1.0)
        print("敲密码…")
        type_text(f, pw, delay=0.08)
        time.sleep(0.6)
        key(f, "ret")
        print("等 12 秒看进没进桌面…")
        time.sleep(12)
        shot(f, "/mnt/e/VMs/Win11/logs/unlock2.ppm")
    finally:
        s.close()
    subprocess.run(["python3", "/mnt/e/VMs/_tools/ppm2png_fast.py",
                    "/mnt/e/VMs/Win11/logs/unlock2.ppm", "/mnt/e/VMs/Win11/logs/unlock2.png"],
                   capture_output=True, text=True)
    print("截图: /mnt/e/VMs/Win11/logs/unlock2.png")


if __name__ == "__main__":
    main()
