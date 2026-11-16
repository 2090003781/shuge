#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""core 启动器（供 reasonix 之外手动 / 计划任务调用）

⚠️ 必须在 reasonix 之外运行（手动双击、计划任务、Windows 服务）。

  在 reasonix 工具调用里运行它没用 —— core 会随工具调用结束被回收：
  reasonix 把每次工具调用放进 Windows Job Object（KILL_ON_JOB_CLOSE），
  且禁止 breakaway（CREATE_BREAKAWAY_FROM_JOB → [WinError 5] 拒绝访问）。
  实测：工具调用内启动的 core 存活 15~110s 后必然消失。
  详见 Vault「记忆/错误库.md #44」与「系统设计/功能更新/2026-09-10-hook自动拉起core.md」。

📌 日常不需要这个脚本：会话开始时 hook（~/.reasonix/logs/hook_logger.py 的
   ensure_core）会自动按需拉起 core 取上下文包，取完即回收（预期行为）。
   只有想让枢阁（笔记软件界面）全天可用时，才用它常驻一份。

用法：
  python <平台目录>\\start_core.py
  停止：打开 http://127.0.0.1:8790/console 点「停止内核」（或 taskkill /F /IM core.exe 强杀）
  软件：http://127.0.0.1:8790（内核 core 在它后面，内部端口 8795）
"""
import os
import subprocess
import sys
import time
import urllib.request

# 平台根从**本脚本位置**推（不再写死盘符目录；2026-10-06 辞玖：不要写死路径）
CORE_DIR = os.path.dirname(os.path.abspath(__file__))
CORE_EXE = os.path.join(CORE_DIR, "bin", "core-host.exe")
LOG = os.path.join(CORE_DIR, "data", "core-run.log")

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# 实测记录（2026-09-10）：
#   普通启动（shell 后代）  → CTRL_CLOSE_EVENT(0xC000013A)，core 优雅退出，存活 15~110s
#   DETACHED_PROCESS        → 硬杀（无「优雅停止」日志），存活 ~60s，指向 job 被关
#   + BREAKAWAY_FROM_JOB    → 本次验证：期望彻底脱离 reasonix 的 job object
BASE_FLAGS = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


def already_running() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8790/api/status", timeout=1.5) as r:  # 主进程前门（内核在则由它转发）
            return r.status == 200
    except Exception:
        return False


def _spawn(log):
    """启动 core；优先 BREAKAWAY_FROM_JOB 脱离 reasonix 的 job object。"""
    argv = [CORE_EXE]  # 无参数 = 确保主进程常驻并巡检/拉起内核（不再直接拉 core.exe）
    kwargs = dict(
        cwd=CORE_DIR,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        close_fds=True,
    )
    try:
        p = subprocess.Popen(argv, creationflags=BASE_FLAGS | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        print("[start] BREAKAWAY_FROM_JOB：已脱离 reasonix 的 job")
        return p
    except OSError as e:
        print(f"[start] BREAKAWAY 失败（{e}），回退普通 DETACHED（不保证常驻）")
    try:
        return subprocess.Popen(argv, creationflags=BASE_FLAGS, **kwargs)
    except OSError as e:
        print(f"[start] 启动失败：{e}")
        return None


def main() -> int:
    if already_running():
        print("枢阁已在运行（8790 响应 200），未重复启动")
        return 0

    log = open(LOG, "ab", buffering=0)
    p = _spawn(log)
    if p is None:
        return 1

    for _ in range(20):
        time.sleep(0.5)
        if already_running():
            print(f"枢阁主进程已启动 (pid={p.pid})，界面 http://127.0.0.1:8790")
            return 0

    print(f"wrapper pid={p.pid} 已创建，但 8790 未就绪，请查看 {LOG}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
