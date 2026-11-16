#!/usr/bin/env python3
"""让客人在它自己里面跑一条命令，把输出拿回来（走 QEMU guest agent）。

用法: python3 vm-exec.py "<PowerShell 命令>" [超时秒]
"""
import json
import socket
import sys
import time

HOST = "127.0.0.1"
PORT = 4444


def connect():
    s = socket.create_connection((HOST, PORT), timeout=8)
    f = s.makefile("rwb")
    f.readline()
    return s, f


def cmd(f, obj, timeout=30):
    f.write((json.dumps(obj) + "\n").encode())
    f.flush()
    end = time.time() + timeout
    while time.time() < end:
        f._sock.settimeout(max(1, end - time.time())) if hasattr(f, "_sock") else None
        try:
            line = f.readline()
        except Exception:
            return None
        if not line:
            return None
        try:
            m = json.loads(line)
        except Exception:
            continue
        if "return" in m or "error" in m:
            return m
    return None


def main():
    ps = sys.argv[1] if len(sys.argv) > 1 else "hostname"
    wait = float(sys.argv[2]) if len(sys.argv) > 2 else 40
    s, f = connect()
    s.settimeout(60)
    cmd(f, {"execute": "qmp_capabilities"})
    # 有没有 guest agent
    st = cmd(f, {"execute": "query-status"})
    print("QMP:", st.get("return") if st else "?")
    r = cmd(f, {"execute": "guest-ping"}, timeout=10)
    print("guest-ping:", r)
    if not r or "return" not in r:
        print("客人里没装/没起 qemu-guest-agent —— 这条路也不通")
        s.close()
        return 1
    path = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    arg = ["-NoProfile", "-Command", ps]
    r = cmd(f, {"execute": "guest-exec", "arguments": {"path": path, "arg": arg, "capture-output": True}}, timeout=20)
    print("guest-exec:", r)
    if not r or "return" not in r:
        print("起不来")
        s.close()
        return 1
    pid = r["return"]["pid"]
    out = None
    end = time.time() + wait
    while time.time() < end:
        time.sleep(0.5)
        r = cmd(f, {"execute": "guest-exec-status", "arguments": {"pid": pid}}, timeout=10)
        if r and r.get("return", {}).get("exited"):
            out = r["return"]
            break
    if out is None:
        print("等超时了（%ss）" % wait)
    else:
        import base64
        so = base64.b64decode(out.get("out-data", "") or "").decode("utf-8", "replace")
        se = base64.b64decode(out.get("err-data", "") or "").decode("utf-8", "replace")
        print("--- 输出 ---")
        print(so)
        if se:
            print("--- 错误 ---")
            print(se)
        print("--- 退出码", out.get("exitcode"), "---")
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
