#!/usr/bin/env python3
"""日志记录插件（独立进程）——原内核内 internal/logsink 的外移版。

职责（2026-09-12 检修步 3）：
    统一行格式 / 级别推断 / 按大小轮转 / 落盘 data/logs/<source>.log —— 这些"记什么、怎么记"
    现在归本插件；内核只保留"把子进程的行转发过来"与"插件不可达时本地兜底直写"。

数据流：
    插件进程 stdout/stderr
      → supervisor 持有管道（Windows 上只有父进程拿得到）
      → 内核 logforward：拆行入队，每 250ms 成批
      → 平台内部调用通道（tool=log.append）
      → 本插件：格式化 + 轮转 + 落盘

行格式（与内核侧兜底逐字一致，保证"谁写的"不影响可读性）：
    2026-09-12T17:58:01+08:00 | INFO  | plugin.context-pack | 消息

旁路原则：任何失败只打印与计数，绝不回错误让内核反复重试；轮转/异常会在分账里留一条详情。
"""
import argparse
import json
import os
import re
import signal
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))

from detail_ledger import DetailLedger  # noqa: E402
from probe import Probe, auth_headers  # noqa: E402

CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:8790")
CORE_DIR = os.environ.get("CORE_DIR", os.path.dirname(HERE))
LOG_DIR = os.path.join(CORE_DIR, "data", "logs")

PLUGIN_ID = "plugin.log"
APPEND_TOOL = "log.append"
MAX_BYTES = 32 << 20   # 与内核侧一致：32MB
KEEP = 5
# 运行期覆盖（--max-mb / --keep）：只为**演练**——32MB 阈值平时碰不到，没旋钮就只能改代码再改回来。
MAX_BYTES_OVERRIDE = 0
KEEP_OVERRIDE = 0

PARENT_CHECK_SECONDS = 5
_PARENT_PID = os.getppid()

running = True
_stats = {"batches": 0, "lines": 0, "bytes": 0, "rotations": 0, "errors": 0, "rejects": 0, "sources": {}}
_DETAIL = None


def handle_stop(signum, frame):
    global running
    print(f"[log] 收到信号 {signum}，优雅退出", flush=True)
    running = False


def _pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def http_post(path, payload, timeout=10):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(CORE_URL + path, data=body,
                                 headers=auth_headers({"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def safe_name(text):
    """来源 id → 安全文件名（与内核侧 safeName 同规则，防路径穿越）"""
    keep = re.sub(r"[^A-Za-z0-9._-]", "_", text or "")
    return keep or "unknown"


def _rfc3339():
    off = time.strftime("%z")  # +0800
    if len(off) == 5:
        off = off[:3] + ":" + off[3:]
    return time.strftime("%Y-%m-%dT%H:%M:%S") + off


def level_of(line):
    """级别粗判（插件只需正常 print，分级由平台兜——现在由本插件兜）"""
    low = line.lower()
    if any(k in low for k in ("traceback", "error", "exception", "failed")) or \
       any(k in line for k in ("失败", "错误", "异常")):
        return "ERROR"
    if "warn" in low or any(k in line for k in ("警告", "降级")):
        return "WARN "
    return "INFO "


def detail_ledger():
    global _DETAIL
    if _DETAIL is None:
        try:
            _DETAIL = DetailLedger(PLUGIN_ID)
            print(f"[log] 分账已就绪 → {_DETAIL.dir}", flush=True)
        except Exception as e:
            print(f"[log] 分账不可用（忽略，不影响主流程）: {e}", flush=True)
            _DETAIL = False
    return _DETAIL or None


# ── 轮转写入（语义与内核侧 internal/logging 一致：<name> → <name>.1 → … → <name>.KEEP）──
class Rotator:
    def __init__(self, path, max_bytes=MAX_BYTES, keep=KEEP):
        self.path = path
        self.max_bytes = int(max_bytes or MAX_BYTES)
        self.keep = int(keep or KEEP)
        self.size = os.path.getsize(path) if os.path.exists(path) else 0
        self.f = open(path, "a", encoding="utf-8", buffering=1)

    def write(self, text):
        data = text.encode("utf-8")
        if self.size + len(data) > self.max_bytes and self.size > 0:
            self._rotate()
        self.f.write(text)
        self.size += len(data)
        return len(data)

    def _rotate(self):
        try:
            self.f.close()
        except Exception:
            pass
        for i in range(self.keep - 1, 0, -1):
            old, nxt = f"{self.path}.{i}", f"{self.path}.{i + 1}"
            if os.path.exists(old):
                if os.path.exists(nxt):
                    os.remove(nxt)
                os.rename(old, nxt)
        if os.path.exists(self.path):
            os.rename(self.path, self.path + ".1")
        self.size = 0
        self.f = open(self.path, "a", encoding="utf-8", buffering=1)
        _stats["rotations"] += 1
        d = detail_ledger()
        if d:
            try:
                d.append("rotate", {"file": os.path.basename(self.path), "max_bytes": self.max_bytes})
            except Exception:
                pass
        print(f"[log] 轮转：{os.path.basename(self.path)} → .1（累计 {_stats['rotations']} 次）", flush=True)

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


_writers = {}


def writer_for(source):
    s = safe_name(source)
    w = _writers.get(s)
    if w is None:
        os.makedirs(LOG_DIR, exist_ok=True)
        w = Rotator(os.path.join(LOG_DIR, s + ".log"),
                     MAX_BYTES_OVERRIDE or MAX_BYTES, KEEP_OVERRIDE or KEEP)
        _writers[s] = w
    return s, w


# ── 平台调用入口 ────────────────────────────────────────────────
def append_log(args):
    """内核转发来的一批行：{source, lines:[...]} → 统一格式落盘"""
    source = str(args.get("source") or "unknown")
    lines = args.get("lines") or []
    if not isinstance(lines, list) or not lines:
        _stats["rejects"] += 1
        return {"ok": False, "error": "lines 为空", "lines": _stats["lines"]}
    # 时间戳与内核侧兜底**逐字一致**（RFC3339 带冒号偏移）：统一格式不能因为"谁写的"而变
    ts = _rfc3339()
    name, w = writer_for(source)
    buf = []
    written = 0
    for raw in lines:
        text = "" if raw is None else str(raw)
        buf.append(f"{ts} | {level_of(text)} | {source} | {text}\n")
    try:
        written = w.write("".join(buf))
    except Exception as e:
        _stats["errors"] += 1
        print(f"[log] 落盘失败 {name}: {e}", flush=True)
        d = detail_ledger()
        if d:
            try:
                d.append("error", {"source": source, "err": f"{type(e).__name__}: {e}", "lines": len(lines)})
            except Exception:
                pass
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    _stats["batches"] += 1
    _stats["lines"] += len(lines)
    _stats["bytes"] += written
    s = _stats["sources"].setdefault(name, {"lines": 0, "bytes": 0})
    s["lines"] += len(lines)
    s["bytes"] += written
    return {"ok": True, "source": name, "lines": len(lines), "bytes": written,
            "total_lines": _stats["lines"]}


def log_stats(_args=None):
    files = {}
    if os.path.isdir(LOG_DIR):
        for fn in sorted(os.listdir(LOG_DIR)):
            if fn.endswith(".log"):
                p = os.path.join(LOG_DIR, fn)
                files[fn] = os.path.getsize(p)
    out = dict(_stats)
    out["dir"] = LOG_DIR
    out["files"] = files
    out["max_bytes"] = MAX_BYTES
    out["keep"] = KEEP
    return out


def _probe_stats():
    return {"batches": _stats["batches"], "lines": _stats["lines"],
            "rotations": _stats["rotations"], "errors": _stats["errors"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default=PLUGIN_ID)
    ap.add_argument("--max-mb", type=float, default=0, help="演练用：覆盖轮转阈值（MB）")
    ap.add_argument("--keep", type=int, default=0, help="演练用：覆盖保留份数")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)
    os.makedirs(LOG_DIR, exist_ok=True)
    global MAX_BYTES_OVERRIDE, KEEP_OVERRIDE
    if args.max_mb > 0:
        MAX_BYTES_OVERRIDE = int(args.max_mb * 1024 * 1024)
        print(f"[log] 演练模式：轮转阈值被覆盖为 {args.max_mb}MB", flush=True)
    if args.keep > 0:
        KEEP_OVERRIDE = int(args.keep)

    try:
        http_post("/api/bridge/plugins/register", {
            "id": args.id, "name": "日志记录",
            "description": "接收内核转发来的插件输出，统一行格式（时间 | 级别 | 来源 | 内容）与级别判定，"
                           "按 32MB 自动轮转、保留 5 份，落到 data/logs/<来源>.log；"
                           "内核侧的兜底直写只在我不在时接手（行内会标 [兜底写入]）。",
            "capabilities": ["log.write", "log.rotate"],
            "produces": [], "consumes": [],
        })
        print(f"[log] 注册成功 {args.id}", flush=True)
    except Exception as e:
        print(f"[log] 注册失败（平台未就绪，稍后由探针周期兜住）: {e}", flush=True)

    probe = Probe(args.id, handlers={APPEND_TOOL: append_log, "log.stats": log_stats},
                  stats_provider=_probe_stats, core_url=CORE_URL)
    probe.start()

    print(f"[log] 日志记录插件已启动：dir={LOG_DIR}（格式 ts | LEVEL | source | text；"
          f"{MAX_BYTES >> 20}MB 轮转，留 {KEEP} 份）", flush=True)
    next_parent = time.time() + PARENT_CHECK_SECONDS
    while running:
        time.sleep(0.5)
        now = time.time()
        if now >= next_parent:
            next_parent = now + PARENT_CHECK_SECONDS
            if not _pid_alive(_PARENT_PID):
                print("[log] 父进程(core)已退出 → 自行退出，避免孤儿", flush=True)
                break

    try:
        probe.stop()
    except Exception:
        pass
    for w in _writers.values():
        w.close()
    print(f"[log] 正常退出（{_stats['lines']} 行 / {_stats['batches']} 批 / 轮转 {_stats['rotations']} 次）", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
