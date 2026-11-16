#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""把 staged 的 host.new.exe 换入现役位置（等旧进程真退了再动文件）。

2026-10-06 踩过：`shutil.move` 到已存在的目标 → FileExistsError；退回到 copy 又撞
PermissionError（旧宿主还攥着 exe）。正确顺序：**杀 → 轮询到进程真没 → 删旧文件 → 移入 → 拉起**。
"""
import os
import shutil
import subprocess
import sys
import time

import psutil

BIN = r"D:\core\bin"
STAGED = os.path.join(BIN, "host.new.exe")
FINAL = os.path.join(BIN, "core-host.exe")


def hosts():
    out = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if (p.info.get("name") or "").lower() == "core-host.exe":
                out.append(p.info["pid"])
        except Exception:
            pass
    return out


def main():
    if not os.path.isfile(STAGED):
        print("没有 staged 产物（%s）—— 先跑 build_exe.ps1 -Target host" % STAGED)
        return 1
    for _ in range(3):
        for pid in hosts():
            try:
                psutil.Process(pid).kill()
                print("  杀掉旧宿主 pid=%s" % pid)
            except Exception as e:
                print("  杀 %s 失败：%s" % (pid, e))
        for _ in range(40):                       # 最多等 20 秒真退干净
            time.sleep(0.5)
            if not hosts():
                break
        if not hosts():
            break
    if hosts():
        print("❌ 还有 core-host.exe 没退：%s（先查谁在拉它，任务每 2 分钟一次）" % hosts())
        return 1
    ok = False
    last = None
    for _ in range(40):                           # 文件句柄可能比进程晚一点释放
        # ⚠️ 每轮都再看一眼：计划任务（每 2 分钟）可能在这中间又把宿主拉起来，文件就又被占住
        again = hosts()
        if again:
            for pid in again:
                try:
                    psutil.Process(pid).kill()
                    print("  （重试期间又冒出一个宿主 pid=%s，已再杀）" % pid)
                except Exception:
                    pass
            time.sleep(1.0)
            continue
        try:
            if os.path.isfile(FINAL):
                os.remove(FINAL)
            shutil.move(STAGED, FINAL)
            ok = True
            break
        except Exception as e:
            last = e
            time.sleep(0.5)
    if not ok:
        print("❌ 换文件失败：%s" % last)
        return 1
    print("  已换入（%.1f MB）" % (os.path.getsize(FINAL) / 1048576))
    subprocess.Popen([FINAL, "--by=命令行（换件）"], cwd=r"D:\core",
                     creationflags=0x08000000 | 0x00000008, close_fds=True)
    print("  已拉起")
    return 0


if __name__ == "__main__":
    sys.exit(main())
