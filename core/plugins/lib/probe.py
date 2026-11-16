#!/usr/bin/env python3
"""心跳点名应答器（external 插件共用件）——平台按周期点名，插件回执。

模型（辞玖 2026-09-11 定：**心跳与点名合并成一个功能**，方向取 pull）：
    平台 → POST /api/bridge/plugins/call {plugin, tool:"plugin.ping"}   （进待办队列）
    插件 → GET  /api/bridge/plugins/poll?plugin=<id>&wait=20            （长轮询取活）
    插件 → POST /api/bridge/plugins/result {callId, ok, data, error}     （回执）

一次点名同时拿到三样东西：
    · 活性：答没答
    · 功能：handlers 注册的业务能力（点名时也能被调用，如 context.get）
    · 状态快照：回执 data 里的 stats → 平台台账 / /api/status 活性快照

回执三态（关键）：
    pong —— 在岗且空闲
    busy —— 在岗但正在干长活（**不算失败**，平台会把下次点名推迟一个周期，不追着问）
    （无应答）—— 平台按失败累计：3 次 → suspect + 交 supervisor 拉起；6 次 → dead

旁路原则：轮询/回执任何失败只打印计数，绝不阻断插件主流程。
"""
import json
import os
import threading
import urllib.parse
import urllib.request

# 8790 = 枢阁（主进程）的唯一地址（前门）；内核在世界观里是它的附属进程，内部端口 8795 不对外开放。
CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:8790")
# 插件面鉴权：内核 spawn 插件时经 PLUGIN_TOKEN 注入（见 internal/pluginauth）。
# 没注入也不报错（单机调试/未启用鉴权时内核会放行），但生产应由内核注入。
PLUGIN_TOKEN = os.environ.get("PLUGIN_TOKEN", "")


def auth_headers(extra=None):
    """统一构造请求头：带插件面 token（有才带）"""
    h = {}
    if PLUGIN_TOKEN:
        h["X-Plugin-Token"] = PLUGIN_TOKEN
    if extra:
        h.update(extra)
    return h


def http_post(path, payload, timeout=10):
    """往平台**前门** POST 一次（带插件面 token）。插件注册、唤醒别的插件都走它。

    2026-10-04 补：`transcript_plugin.main()` 一直写着 `from probe import http_post`，
    可这个名字压根不存在 → 每次都 ImportError 被 except 吞掉（只留一句"注册失败"）。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(CORE_URL + path, data=body,
                                 headers=auth_headers({"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class Probe:
    """平台点名应答器（后台线程长轮询取活）"""

    def __init__(self, plugin_id, handlers=None, stats_provider=None,
                 busy_provider=None, wait=20, core_url=None, quiet=False):
        self.plugin_id = plugin_id
        self.handlers = dict(handlers or {})   # tool → fn(args) → data
        self.stats_provider = stats_provider   # () → dict
        self.busy_provider = busy_provider     # () → bool
        self.wait = int(wait)
        self.core = core_url or CORE_URL
        self.quiet = quiet
        self.polls = 0
        self.calls_handled = 0
        self.errors = 0
        self._stop = threading.Event()
        self._thread = None

    # ── 对外 ────────────────────────────────────────────────
    def register_handler(self, tool, fn):
        """注册业务能力（点名时或平台 call 时被调用）"""
        self.handlers[tool] = fn

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="probe")
        self._thread.start()
        self._log("点名应答已启动（wait=%ss 能力=%s）" % (self.wait, ",".join(self.handlers) or "-"))

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def stats(self):
        return {"polls": self.polls, "calls_handled": self.calls_handled, "probe_errors": self.errors}

    # ── 内部 ────────────────────────────────────────────────
    def _log(self, msg):
        if not self.quiet:
            print("[probe:%s] %s" % (self.plugin_id, msg), flush=True)

    def _loop(self):
        while not self._stop.is_set():
            try:
                calls = self._poll()
            except Exception as e:
                self.errors += 1
                self._log("poll 失败（平台未就绪？）: %s" % e)
                self._stop.wait(2.0)
                continue
            for call in calls:
                self._handle(call)

    def _poll(self):
        url = "%s/api/bridge/plugins/poll?plugin=%s&wait=%d" % (
            self.core, urllib.parse.quote(self.plugin_id), self.wait)
        req = urllib.request.Request(url, headers=auth_headers({"Accept": "application/json"}))
        with urllib.request.urlopen(req, timeout=self.wait + 5) as r:
            data = json.loads(r.read().decode("utf-8"))
        self.polls += 1
        return data.get("calls") or []

    def _handle(self, call):
        call_id = call.get("callId", "")
        tool = call.get("tool", "")
        args = call.get("args") or {}
        ok, data, err = True, None, ""
        try:
            if tool == "plugin.ping":
                data = self._ping_payload()
            elif tool in self.handlers:
                data = self.handlers[tool](args)
            else:
                ok, err = False, "unknown tool: %s" % tool
        except Exception as e:
            ok, err = False, "%s: %s" % (type(e).__name__, e)
        self.calls_handled += 1
        self._result(call_id, ok, data, err)

    def _ping_payload(self):
        """点名回执：status(pong/busy) + stats（喂平台活性快照）"""
        busy = False
        try:
            busy = bool(self.busy_provider()) if self.busy_provider else False
        except Exception:
            busy = False
        stats = {}
        try:
            stats = dict(self.stats_provider() or {}) if self.stats_provider else {}
        except Exception as e:
            stats = {"stats_error": "%s: %s" % (type(e).__name__, e)}
        stats["status"] = "busy" if busy else "pong"
        stats.update(self.stats())
        return stats

    def _result(self, call_id, ok, data, err):
        if not call_id:
            return
        body = json.dumps({"callId": call_id, "ok": ok, "data": data, "error": err},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.core + "/api/bridge/plugins/result", data=body,
                                     headers=auth_headers({"Content-Type": "application/json"}))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read()
        except Exception as e:
            self.errors += 1
            self._log("回执失败（平台可能重启中）: %s" % e)
