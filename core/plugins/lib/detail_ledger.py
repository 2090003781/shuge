#!/usr/bin/env python3
"""分账写入助手（external 插件共用件）——总账记「状态」，分账记「详情」。

模型（辞玖 2026-09-11 定）：
    总账  data/ledger/ledger-*.jsonl                     ← 状态行（平台写，**权威**）
    分账  data/ledger/detail/<owner>/<owner>-*.jsonl      ← 详情（本助手写，**证据**）

规则（与 Go 侧 internal/ledger 对齐，两边同一套约定）：
    · 每行一个 JSON：{seq, ts, owner, action, kind, payload}
    · seq 按 owner 单调递增；启动读末行续号；**末尾半行就地截断**
    · 组提交：攒 commit_every_ms 或 commit_max 条 → 一次 write + 一次 fsync
    · 轮转：单文件超 max_bytes 换新文件；**永久保留**（暂不按时间清理）
    · 单写者：只有本 owner 的进程写自己的文件
    · **分账是证据不是权威**：任何失败只打印计数，绝不阻断插件主流程

用法：
    from detail_ledger import DetailLedger
    det = DetailLedger("plugin.context-pack")            # 目录默认 data/ledger/detail/
    ref = det.append("context.pack.write", payload={...}, action_id="act.1")
    # ref = {"owner":…, "file":…, "seq":…, "lines":1, "bytes":123}
    # 把 ref 放进上报给平台的状态行 payload.ref → 总账即可定位详情（两级查询第二步）
"""
import json
import os
import threading
import time
from datetime import datetime

DEFAULT_BASE = os.environ.get("LEDGER_DIR", os.path.join("data", "ledger"))


def _safe(name):
    """owner → 安全目录名（与 Go 侧 safeOwner 同规则）"""
    out = []
    for ch in name:
        out.append(ch if (ch.isalnum() or ch in ".-_") else "_")
    return "".join(out) or "unknown"


class DetailLedger:
    """单 owner 分账写入器：线程安全 · 组提交 · 永久保留"""

    def __init__(self, owner, base=None, commit_every_ms=10, commit_max=200,
                 max_bytes=64 * 1024 * 1024, quiet=False):
        self.owner = owner
        self.base = base or DEFAULT_BASE
        self.dir = os.path.join(self.base, "detail", _safe(owner))
        self.commit_every = max(1, int(commit_every_ms)) / 1000.0
        self.commit_max = max(1, int(commit_max))
        self.max_bytes = int(max_bytes)
        self.quiet = quiet
        self._lock = threading.Lock()
        self._pending = []      # [(rec, line_bytes)]
        self._seq = 0
        self._size = 0
        self._path = None
        self._fh = None
        self.written = 0
        self.write_errors = 0
        self.rotations = 0
        os.makedirs(self.dir, exist_ok=True)
        self._open()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ── 对外 ────────────────────────────────────────────────
    def append(self, kind, payload=None, action_id=""):
        """写一条详情，返回 ref（放进上报给平台的状态行 payload.ref）"""
        rec = {
            "seq": 0,  # 锁内分配
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "owner": self.owner,
            "action": action_id or "",
            "kind": kind,
            "payload": payload if payload is not None else {},
        }
        with self._lock:
            self._seq += 1
            rec["seq"] = self._seq
            seq = self._seq
            line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
            self._pending.append((rec, line))
            need = len(self._pending) >= self.commit_max
            fname = os.path.basename(self._path or "")
        if need:
            self._flush()
        return {"owner": self.owner, "file": fname, "seq": seq, "lines": 1, "bytes": len(line)}

    def flush(self):
        self._flush()

    def close(self):
        self._flush()
        self._stop.set()
        try:
            self._thread.join(timeout=2)
        except Exception:
            pass
        with self._lock:
            if self._fh:
                try:
                    self._fh.flush()
                    os.fsync(self._fh.fileno())
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None

    def stats(self):
        with self._lock:
            return {
                "owner": self.owner, "dir": self.dir,
                "file": os.path.basename(self._path or ""),
                "seq": self._seq, "pending": len(self._pending),
                "written": self.written, "write_errors": self.write_errors,
                "rotations": self.rotations,
            }

    # ── 内部 ────────────────────────────────────────────────
    def _loop(self):
        while not self._stop.wait(self.commit_every):
            self._flush()

    def _open(self):
        """续写最新分账文件（末尾半行截断、seq 续号）；没有则新建"""
        names = sorted(n for n in os.listdir(self.dir) if n.endswith(".jsonl"))
        path = None
        if names:
            last = os.path.join(self.dir, names[-1])
            self._seq = self._read_last_seq(last)   # 内部会截断半行
            if os.path.getsize(last) < self.max_bytes:
                path = last
        if path is None:
            path = os.path.join(self.dir, "%s-%s.jsonl" % (_safe(self.owner), time.strftime("%Y%m%d-%H%M%S")))
        self._path = path
        self._size = os.path.getsize(path) if os.path.exists(path) else 0
        self._fh = open(path, "ab")

    def _read_last_seq(self, path):
        """读末条完整记录的 seq；文件末尾半行就地截断（只截尾，不废整文件）"""
        try:
            fd = os.open(path, os.O_RDWR)
        except Exception:
            return 0
        try:
            size = os.lseek(fd, 0, os.SEEK_END)
            if size == 0:
                return 0
            start = max(0, size - 262144)
            os.lseek(fd, start, os.SEEK_SET)
            buf = os.read(fd, size - start)
            if not buf.endswith(b"\n"):
                idx = buf.rfind(b"\n")
                new_size = (start + idx + 1) if idx >= 0 else start
                os.ftruncate(fd, new_size)
                buf = buf[:idx + 1] if idx >= 0 else b""
            for ln in reversed([x for x in buf.split(b"\n") if x.strip()][-200:]):
                try:
                    return int(json.loads(ln.decode("utf-8")).get("seq", 0))
                except Exception:
                    continue
        except Exception:
            return 0
        finally:
            os.close(fd)
        return 0

    def _flush(self):
        with self._lock:
            if not self._pending:
                return
            batch = self._pending
            self._pending = []
        data = b"".join(line for _, line in batch)
        try:
            if self._size + len(data) > self.max_bytes:
                self._rotate()
            self._fh.write(data)
            self._fh.flush()
            os.fsync(self._fh.fileno())
            with self._lock:
                self._size += len(data)
                self.written += len(batch)
        except Exception as e:
            self.write_errors += 1
            if not self.quiet:
                print("[detail-ledger] %s 写失败（详情可丢，权威在总账）: %s" % (self.owner, e), flush=True)

    def _rotate(self):
        try:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
        except Exception:
            pass
        self._path = os.path.join(self.dir, "%s-%s.jsonl" % (_safe(self.owner), time.strftime("%Y%m%d-%H%M%S")))
        self._fh = open(self._path, "ab")
        self._size = 0
        self.rotations += 1
