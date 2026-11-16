# -*- coding: utf-8 -*-
"""\u7ed9\u754c\u9762\u62cd\u5f20\u7167\uff08\u65e0\u5934 Edge\uff09\u2014\u2014 \u6539\u5b8c\u7248\u5f0f\u81ea\u5df1\u5148\u770b\u4e00\u773c\uff0c\u4e0d\u7528\u7b49\u4eba\u53cd\u9988\u3002

\u7528\u6cd5\uff1apython tools/shot_ui.py [\u8f93\u51fa.png] [url]
\u9ed8\u8ba4\uff1adata/logs/ui.png + http://127.0.0.1:8848/
"""
import os
import subprocess
import sys

EDGE = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "data/logs/ui.png"
    url = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8848/"
    exe = next((p for p in EDGE if os.path.isfile(p)), None)
    if not exe:
        print("\u627e\u4e0d\u5230 Edge\uff0c\u8df3\u8fc7")
        return 0
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    # --virtual-time-budget：让页面把树/统计都跑完再截，否则截到的是"加载中…"（2026-09-28）
    budget = os.environ.get("SHOT_BUDGET") or "9000"
    r = subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--force-device-scale-factor=1", "--window-size=1500,900",
                        "--virtual-time-budget=" + budget,
                        "--screenshot=" + os.path.abspath(out), url],
                       capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW)
    ok = os.path.isfile(out) and os.path.getsize(out) > 1000
    print("%s \u2192 %s\uff08%d \u5b57\u8282\uff09" % ("\u2705" if ok else "\u274c", out,
                                              os.path.getsize(out) if os.path.isfile(out) else 0))
    if not ok and r.stderr:
        print(r.stderr[-300:])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
