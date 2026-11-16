#!/usr/bin/env python3
"""通道型插件共用件：**插件背书的 agent**（平台 ↔ 某个接入方之间的定向投递通道）。

谁在用它：plugin.dsh（DSH 通道）、plugin.reasonix（Reasonix 通道）——
它们的差别只有 id/名字/能力/inbox 目录，逻辑完全同构，所以抽成一件。

协议要点（2026-09-12 定，配合内核 internal/bridge/external_agent.go）：
    1. 注册时声明 agent_id（如 agent.dsh）→ 平台登记"插件背书的 agent"，
       组件列表/拓扑/Inject(target=…) 对外行为与内核内适配器时代完全一致；
    2. 平台定向投递 → 走插件调用通道（长轮询），tool=agent.deliver，args={"event": {...}}；
    3. 本件把事件落 inbox 文件（各接入方照旧轮询取件）——文件格式：
       {"type","source","target","time","payload"}，UTF-8 JSON，缩进 2；
    4. 探针应答 + 分账（每次投递一条详情）+ 父进程看护 + 周期幂等重注册。

旁路原则：落盘/分账失败只打印与计数，不阻断（回执里如实带 ok=false 与 error）。

**「日志记录」（2026-10-02 加）**：每个 agent 接口自带 `agent.record` —— 但它是**薄入口**：
只把 agent 上报的事件**追加到本 agent 的 spool**（`data/transcripts/<agent>/<日期>.jsonl`，一行一个），
然后立刻回执；顺手"唤醒"独立插件 `plugin.transcript`（发出去就不管，失败无所谓）。
**组装成一天一份、写进库**那件慢活归 `plugin.transcript`。
为什么薄：hook 只有 5 秒预算 —— "接住"不能依赖"下游活着"，否则投递给转录当人质。
"""
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.request

CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:8790")
PLUGIN_TOKEN = os.environ.get("PLUGIN_TOKEN", "")


def auth_headers(extra=None):
    """统一构造请求头：带插件面 token（有才带）。插件面鉴权见 internal/pluginauth。"""
    h = {}
    if PLUGIN_TOKEN:
        h["X-Plugin-Token"] = PLUGIN_TOKEN
    if extra:
        h.update(extra)
    return h

PARENT_CHECK_SECONDS = 5
REREGISTER_SECONDS = 60

# 「日志记录」的唤醒队列上限：满了就丢（spool 才是兜底，唤醒只是让它更早动手）
WAKE_QUEUE_MAX = 1000


def _pid_alive(pid):
    """跨平台判进程存活（零依赖）"""
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
        return bool(ok) and code.value == 259  # STILL_ACTIVE
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
    """事件类型 → 安全文件名片段（与内核侧 safeEventName/safeName 同规则，防路径穿越）"""
    keep = re.sub(r"[^A-Za-z0-9._-]", "_", text or "")
    return keep or "event"


class AgentChannel:
    """一个接入方的通道：注册身份 + 接投递 + 落 inbox + 探针 + 看护"""

    def __init__(self, plugin_id, name, agent_id, agent_name, inbox_dir,
                 capabilities=None, agent_capabilities=None,
                 produces=None, consumes=None, deliver_tool="agent.deliver",
                 status_tool="channel.status", core_url=None, description="",
                 agent_description="",
                 record_dir=None, record_tool=None,
                 attach_tool=None, detach_tool=None,
                 recorder_plugin="plugin.transcript", recorder_tool="transcript.wake"):
        self.plugin_id = plugin_id
        self.name = name
        self.agent_id = agent_id
        self.agent_name = agent_name or agent_id
        self.inbox_dir = inbox_dir
        self.deliver_tool = deliver_tool
        self.status_tool = status_tool
        self.core = core_url or CORE_URL
        self.description = description
        self.agent_description = agent_description
        self.capabilities = capabilities or ["agent.bridge"]
        self.agent_capabilities = agent_capabilities or self.capabilities
        self.produces = produces or []
        self.consumes = consumes or [agent_id]

        # ── 「日志记录」（薄入口；不给 record_dir 就整个功能关掉）──
        self.record_dir = record_dir
        # 工具名**必须一个 agent 一个**：内核 `externalPluginFor` 是按能力名找插件的，
        # 而 plugins 是 map（遍历顺序随机）—— 两个接口同名的话事件会随机投错家（2026-10-02）。
        self.record_tool = record_tool or ("agent.record." + agent_id.rsplit(".", 1)[-1])
        self.recorder_plugin = recorder_plugin
        self.recorder_tool = recorder_tool
        # 「谁挂着我的日志」——转录服务连上来之后记在这儿（显式、可查、可拆）
        # 能力名**一个 agent 一个**（同 record_tool 的道理：内核按能力名路由，
        # 两个接口同名的话"谁挂谁"会随机投错家）
        _sfx = agent_id.rsplit(".", 1)[-1]
        self.attach_tool = attach_tool or ("agent.attach." + _sfx)
        self.detach_tool = detach_tool or ("agent.detach." + _sfx)
        self.attachments = {}
        self.attach_events = 0

        self.running = True
        self.stats = {"delivered": 0, "last_at": 0.0, "last_type": "", "errors": 0,
                      "rejects": 0, "reregisters": 0,
                      "recorded": 0, "record_bytes": 0, "record_rejects": 0,
                      "record_days": [], "wakes": 0, "wake_fails": 0}
        self._detail = None
        self._probe = None
        self._wake_q = None
        self._wake_thread = None

    # ── 分账（旁路）────────────────────────────────────────────
    def detail(self):
        if self._detail is None:
            try:
                from detail_ledger import DetailLedger
                self._detail = DetailLedger(self.plugin_id)
                self.log(f"分账已就绪 → {self._detail.dir}")
            except Exception as e:
                self.log(f"分账不可用（忽略，不影响主流程）: {e}")
                self._detail = False
        return self._detail or None

    def log(self, msg):
        print(f"[{self.plugin_id}] {msg}", flush=True)

    # ── 注册 ──────────────────────────────────────────────────
    def register(self, quiet=False):
        caps = list(self.capabilities)
        for t in (self.attach_tool, self.detach_tool):
            if t not in caps:
                caps.append(t)                 # 能力名 == 工具名（内核按它路由）
        if self.record_dir and self.record_tool not in caps:
            caps.append(self.record_tool)      # 能力名 == 工具名（内核按它路由），所以这儿自己加
        info = http_post("/api/bridge/plugins/register", {
            "id": self.plugin_id,
            "name": self.name,
            "description": self.description,
            "capabilities": caps,
            "produces": self.produces,
            "consumes": self.consumes,
            # ↓ 关键：声明"我背书哪个 agent"，平台据此登记插件背书的 agent
            "agentId": self.agent_id,
            "agentName": self.agent_name,
            "agentCapabilities": self.agent_capabilities,
            "agentDescription": self.agent_description,
            "deliverTool": self.deliver_tool,
        })
        if not quiet:
            self.log(f"注册成功 {self.plugin_id}（背书 {self.agent_id}）")
        return info

    # ── 投递（平台 → 接入方）──────────────────────────────────
    def deliver(self, args):
        raw = args.get("event") or {}
        if not isinstance(raw, dict) or not raw.get("type"):
            self.stats["rejects"] += 1
            return {"ok": False, "error": "event.type 缺失", "delivered": self.stats["delivered"]}
        ev = {
            "type": raw.get("type", ""),
            "source": raw.get("source", ""),
            "target": raw.get("target", self.agent_id),
            "time": raw.get("time") or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "payload": raw.get("payload") or {},
        }
        try:
            os.makedirs(self.inbox_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S") + (".%03d" % int((time.time() % 1) * 1000))
            fname = f"{stamp}_{safe_name(ev['type'])}.json"
            with open(os.path.join(self.inbox_dir, fname), "w", encoding="utf-8") as f:
                json.dump(ev, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.stats["errors"] += 1
            self.log(f"投递落盘失败: {e}")
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "delivered": self.stats["delivered"]}

        self.stats["delivered"] += 1
        self.stats["last_at"] = time.time()
        self.stats["last_type"] = ev["type"]
        d = self.detail()
        ref = None
        if d:
            try:
                ref = d.append("inject", {"type": ev["type"], "source": ev["source"],
                                          "target": ev["target"], "file": fname,
                                          "payload": ev["payload"]})
            except Exception as e:
                self.log(f"分账写入失败（忽略）: {e}")
        self.log(f"已投递 → {fname}（累计 {self.stats['delivered']}）")
        return {"ok": True, "file": fname, "delivered": self.stats["delivered"], "ref": ref}

    # ── 「日志记录」：接住（薄）──────────────────────────────
    def record(self, args):
        """agent 接口的日志记录入口：**追加到 spool + 立刻回执**。

        入参两种写法都收：
            {"event": {...}}        ← 与投递同形（推荐，agent 侧不用记两套）
            {...事件本体...}         ← 直接给事件体

        返回很快（只写一行盘）。**不入库、不组装、不等下游** —— 那些是 plugin.transcript 的事。
        """
        if not self.record_dir:
            self.stats["record_rejects"] += 1
            return {"ok": False, "error": "本通道没开日志记录（record_dir 为空）"}
        # 批量：`{"events": [{...}, {...}]}` —— 采集端（钩子转录服务）一次交一批，
        # 省掉几百次进程间往返（2026-10-04）。逐条仍走下面同一条路，口径不变。
        many = (args or {}).get("events")
        if isinstance(many, list):
            ok = bad = 0
            days = {}
            for one in many:
                r = self.record({"event": one})
                if r.get("ok"):
                    ok += 1
                    days[r.get("day")] = days.get(r.get("day"), 0) + 1
                else:
                    bad += 1
            if ok:
                self.wake_recorder(max(days, key=lambda d: days[d]) if days else None)
            return {"ok": ok > 0, "recorded": ok, "rejects": bad, "days": days,
                    "total": self.stats["recorded"]}
        raw = args.get("event")
        if not isinstance(raw, dict):
            raw = args if isinstance(args, dict) else {}
        if not raw.get("type"):
            self.stats["record_rejects"] += 1
            return {"ok": False, "error": "event.type 缺失", "recorded": self.stats["recorded"]}

        ev = {
            "type": str(raw.get("type") or ""),
            "source": raw.get("source") or self.agent_id,
            "target": raw.get("target") or self.agent_id,
            "agent": self.agent_id,
            "time": raw.get("time") or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "seq": raw.get("seq"),
            "payload": raw.get("payload") if isinstance(raw.get("payload"), dict) else {},
        }
        # 按**事件时间**归天（深夜跨天的事件不会被算到第二天）
        day = str(ev["time"])[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            day = time.strftime("%Y-%m-%d")
        try:
            os.makedirs(self.record_dir, exist_ok=True)
            line = json.dumps(ev, ensure_ascii=False) + "\n"
            path = os.path.join(self.record_dir, day + ".jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            n = len(line.encode("utf-8"))
        except Exception as e:
            self.stats["errors"] += 1
            self.log(f"日志记录落盘失败: {e}")
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                    "recorded": self.stats["recorded"]}

        self.stats["recorded"] += 1
        self.stats["record_bytes"] += n
        if day not in self.stats["record_days"]:
            self.stats["record_days"] = (self.stats["record_days"] + [day])[-7:]
        d = self.detail()
        if d:
            try:
                d.append("record", {"type": ev["type"], "day": day, "bytes": n})
            except Exception:
                pass
        self.wake_recorder(day)          # 发出去就不管
        return {"ok": True, "recorded": self.stats["recorded"], "day": day,
                "spool": os.path.join(os.path.basename(self.record_dir), day + ".jsonl")}

    # ── 「连接」：转录服务挂接 / 拆线（辞玖 2026-10-04）────────
    def _last_record_at(self):
        """最后一条日志是什么时候落的（看 spool 的 mtime，不读内容）"""
        if not self.record_dir or not os.path.isdir(self.record_dir):
            return 0.0
        try:
            return max([os.path.getmtime(os.path.join(self.record_dir, x))
                        for x in os.listdir(self.record_dir) if x.endswith(".jsonl")] or [0.0])
        except Exception:
            return 0.0

    def attach(self, args):
        """转录服务来挂接：**这就是「连接 agent 接口」那一步**。

        入参：`{recorder: "plugin.hook-transcript", note?: "..."}`（谁要录我的日志）
        回执：告诉它该用哪个记录入口、spool 在哪、现在最后一条日志是什么时候 —— 连上就能干活。
        """
        a = args or {}
        who = str(a.get("recorder") or a.get("who") or "").strip()
        if not who:
            return {"ok": False, "error": "缺 recorder（谁要接我的日志）"}
        fresh = who not in self.attachments
        self.attachments[who] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                 "note": str(a.get("note") or "")}
        self.attach_events += 1
        self.log(("已挂接" if fresh else "重复挂接（幂等）") + f"：{who}")
        last = self._last_record_at()
        return {"ok": True, "agent": self.agent_id, "recorder": who,
                "attached": sorted(self.attachments),
                "record_tool": self.record_tool, "status_tool": self.status_tool,
                "spool_dir": self.record_dir,
                "last_record_at": (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(last))
                                   if last else ""),
                "silent_s": (round(time.time() - last, 1) if last else None),
                "attach_events": self.attach_events}

    def detach(self, args):
        """拆线：转录服务要走了（或换人），把它从名单里去掉"""
        who = str((args or {}).get("recorder") or (args or {}).get("who") or "").strip()
        gone = self.attachments.pop(who, None) is not None
        self.log(f"已拆线：{who or '(未写名字)'} gone={gone}")
        return {"ok": True, "agent": self.agent_id, "recorder": who, "removed": gone,
                "attached": sorted(self.attachments)}

    # ── 唤醒转录插件（旁路：失败只计数）──────────────────────
    def wake_recorder(self, day):
        """把"某天有新事件"丢进队列，由后台线程去叫转录插件。

        为什么不在请求线程里直接发：那会把 hook 的 5 秒预算押在下游的 HTTP 往返上。
        """
        if not self.recorder_plugin:
            return
        try:
            if self._wake_q is None:
                import queue
                self._wake_q = queue.Queue(maxsize=WAKE_QUEUE_MAX)
            self._wake_q.put_nowait(day)
        except Exception:
            return                      # 队列满：丢（spool 是兜底，下轮扫描会发现）
        if self._wake_thread is None or not self._wake_thread.is_alive():
            self._wake_thread = threading.Thread(target=self._wake_loop,
                                                 name="record-wake", daemon=True)
            self._wake_thread.start()

    def _wake_loop(self):
        seen = set()
        while self.running:
            try:
                day = self._wake_q.get(timeout=2.0)
            except Exception:
                continue
            if day in seen:             # 同一天 30 秒内只叫一次（够用，且有兜底扫描）
                continue
            seen.add(day)
            if len(seen) > 16:
                seen.clear()
            try:
                http_post("/api/bridge/plugins/call",
                          {"plugin": self.recorder_plugin, "tool": self.recorder_tool,
                           "args": {"agent": self.agent_id, "day": day}, "timeoutMs": 1500})
                self.stats["wakes"] += 1
            except Exception:
                self.stats["wake_fails"] += 1      # 旁路：叫不动就算了

    def status(self, _args=None):
        try:
            n = len(os.listdir(self.inbox_dir)) if os.path.isdir(self.inbox_dir) else 0
        except Exception:
            n = -1
        rec = None
        if self.record_dir:
            try:
                days = sorted(x[:-6] for x in os.listdir(self.record_dir) if x.endswith(".jsonl"))
            except Exception:
                days = []
            rec = {"dir": self.record_dir, "days": days[-7:], "recorded": self.stats["recorded"],
                   "bytes": self.stats["record_bytes"], "rejects": self.stats["record_rejects"],
                   "wakes": self.stats["wakes"], "wake_fails": self.stats["wake_fails"],
                   "recorder": self.recorder_plugin}
        last = self._last_record_at()
        return {
            "agent": self.agent_id,
            "delivered": self.stats["delivered"],
            "last_type": self.stats["last_type"],
            "last_age_s": round(time.time() - self.stats["last_at"], 1) if self.stats["last_at"] else None,
            "inbox_dir": self.inbox_dir,
            "inbox_files": n,
            "record": rec,
            # 「谁挂着我的日志」+「最后一条日志多久没来了」——看门狗该看的判据在这儿
            "attachments": sorted(self.attachments),
            "attachment_detail": self.attachments,
            "last_record_at": (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(last))
                               if last else ""),
            "silent_s": (round(time.time() - last, 1) if last else None),
        }

    def probe_stats(self):
        s = dict(self.stats)
        s["last_age_s"] = round(time.time() - s["last_at"], 1) if s["last_at"] else None
        return s

    # ── 主循环 ────────────────────────────────────────────────
    def run(self, argv=None):
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--id", default=self.plugin_id)
        args = ap.parse_args(argv)

        def _stop(signum, frame):
            self.log(f"收到信号 {signum}，优雅退出")
            self.running = False

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        os.makedirs(self.inbox_dir, exist_ok=True)
        try:
            self.register(args.id)
        except Exception as e:
            self.log(f"注册失败（平台未就绪，稍后重注册）: {e}")

        from probe import Probe
        handlers = {self.deliver_tool: self.deliver, self.status_tool: self.status,
                    self.attach_tool: self.attach, self.detach_tool: self.detach}
        if self.record_dir:                     # 「日志记录」：本方 agent 的日志入口
            handlers[self.record_tool] = self.record
        self._probe = Probe(args.id, handlers=handlers,
                            stats_provider=self.probe_stats, core_url=self.core)
        self._probe.start()

        self.log(f"通道已启动：inbox={self.inbox_dir}，连接入口 tool={self.attach_tool}"
                 f"（转录服务挂接用）；等待平台定向投递（tool={self.deliver_tool}）"
                 + (f"；日志记录已开 → {self.record_dir}（tool={self.record_tool}）" if self.record_dir else ""))
        parent_pid = os.getppid()
        next_parent = time.time() + PARENT_CHECK_SECONDS
        next_register = time.time() + REREGISTER_SECONDS
        while self.running:
            time.sleep(0.5)
            now = time.time()
            if now >= next_parent:
                next_parent = now + PARENT_CHECK_SECONDS
                if not _pid_alive(parent_pid):
                    self.log("父进程(core)已退出 → 自行退出，避免孤儿")
                    break
            if now >= next_register:
                next_register = now + REREGISTER_SECONDS
                try:
                    self.register(args.id, quiet=True)
                    self.stats["reregisters"] += 1
                except Exception:
                    pass  # 平台重启中：下轮再试

        try:
            self._probe.stop()
        except Exception:
            pass
        self.log(f"正常退出（投递 {self.stats['delivered']} 次 / 错误 {self.stats['errors']} 次）")
        sys.exit(0)
