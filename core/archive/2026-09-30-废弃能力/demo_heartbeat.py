#!/usr/bin/env python3
"""示例插件进程（Python）：验证监督式多进程 AD-1

行为：
- 每 1 秒打印一次心跳（心跳计数）
- 支持 --crash-after N：N 秒后主动崩溃退出（模拟插件故障，测试监督器自动重启）
- 收到 SIGTERM/SIGINT 优雅退出（主动停止不应被重启）
"""
import argparse
import signal
import sys
import time

running = True


def handle_stop(signum, frame):
    global running
    print(f"[plugin-demo] 收到信号 {signum}，优雅退出", flush=True)
    running = False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="demo.py")
    ap.add_argument("--crash-after", type=float, default=0, help="N 秒后主动崩溃")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    print(f"[plugin-demo:{args.id}] 启动，PID={__import__('os').getpid()}", flush=True)
    start = time.time()
    beat = 0
    while running:
        time.sleep(1)
        beat += 1
        elapsed = time.time() - start
        print(f"[plugin-demo:{args.id}] 心跳 #{beat}（运行 {elapsed:.0f}s）", flush=True)
        if args.crash_after and elapsed >= args.crash_after:
            print(f"[plugin-demo:{args.id}] 模拟崩溃退出", flush=True)
            sys.exit(1)  # 非零退出 = 异常崩溃 → 监督器应重启
    print(f"[plugin-demo:{args.id}] 正常退出（心跳 {beat} 次）", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
