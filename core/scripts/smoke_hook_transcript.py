# -*- coding: utf-8 -*-
"""钩子转录服务的验收（**离线**：假会话库 + 假 agent 接口，不碰真存储、不联网）。

    python scripts/smoke_hook_transcript.py

验的是辞玖 2026-10-04 定的那条链：

    源适配器（读 DSH 会话存储）──事件──▶ **agent 接口**（agent.record.<agent>）──▶ 库

覆盖：连上接口（agent.attach）/ 首次拉取全部交付给接口 / 幂等（源没变就不读）/
追加后只交新增 / 偏移从 0 重读靠 seq 不重复 / 跨天归天 / since 过滤 /
接管老「推」的 spool（只做一次）/ 看门狗（源在动却没日志 → 告警；恢复 → 解除）/
长时间结果留痕 / reasoning 默认不收 / 直报型 agent（reasonix）不采只挂。
"""
import datetime
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugins"))
sys.path.insert(0, os.path.join(ROOT, "plugins", "lib"))

import zstandard as zstd  # noqa: E402

import hook_sources as HS          # noqa: E402
import hook_transcript_plugin as HT  # noqa: E402
import dsh_session as DS           # noqa: E402

OK = True
FAILED = []


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    if not cond:
        FAILED.append(name)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:170] if extra else ""))


def ms(y, mo, d, h=10, mi=0, s=0):
    return int(datetime.datetime(y, mo, d, h, mi, s).timestamp() * 1000)


def rec(type_, seq, time_ms, data=None, **kw):
    r = {"type": type_, "seq": seq, "time": time_ms, "data": data or {}}
    r.update(kw)
    return r


def write_frames(path, records):
    c = zstd.ZstdCompressor()
    with open(path, "ab") as f:
        for r in records:
            f.write(c.compress((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8")))


DAY1, DAY2 = ms(2099, 1, 1, 9), ms(2099, 1, 2, 9)


class FakeInterface:
    """假装自己是 agent.dsh 那个接口：接 attach、收 record、把事件写进 spool。"""

    def __init__(self, spool_dir, agent_id="agent.dsh"):
        self.spool_dir = spool_dir
        self.agent_id = agent_id
        os.makedirs(spool_dir, exist_ok=True)
        self.attached = {}
        self.records = []
        self.attach_calls = 0
        self.batches = 0

    def call(self, plugin, tool, args, timeout_ms=60000):
        if tool.startswith("agent.attach."):
            self.attach_calls += 1
            self.attached[args.get("recorder")] = True
            return 200, {"result": {"ok": True, "agent": self.agent_id,
                                    "attached": sorted(self.attached),
                                    "record_tool": "agent.record." + self.agent_id.rsplit(".", 1)[-1],
                                    "status_tool": "fake.status",
                                    "spool_dir": self.spool_dir,
                                    "last_record_at": self.last_record_at()}}
        if tool.startswith("agent.record."):
            evs = args.get("events") or ([args["event"]] if args.get("event") else [])
            days = {}
            for e in evs:
                d = str(e.get("time") or "")[:10]
                with open(os.path.join(self.spool_dir, d + ".jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
                days[d] = days.get(d, 0) + 1
                self.records.append(e)
            self.batches += 1
            return 200, {"result": {"ok": bool(evs), "recorded": len(evs), "days": days}}
        return 200, {"result": {"ok": False, "error": "不认识的工具 " + tool}}

    def last_record_at(self):
        fs = [x for x in os.listdir(self.spool_dir) if x.endswith(".jsonl")]
        if not fs:
            return ""
        t = max(os.path.getmtime(os.path.join(self.spool_dir, x)) for x in fs)
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))

    def rd(self, day):
        p = os.path.join(self.spool_dir, day + ".jsonl")
        if not os.path.isfile(p):
            return []
        return [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]


def main():
    tmp = tempfile.mkdtemp(prefix="zz-hooktr-")
    # 状态/暂存都挪到临时目录（别把真状态写花）
    real = (HT.STATE_FILE, HT.SPOOL_DIR, HT.STAGING_DIR, HT._post_call, HT.write_alarm_note)
    HT.STATE_FILE = os.path.join(tmp, "state.json")
    HT.SPOOL_DIR = os.path.join(tmp, "transcripts")
    HT.STAGING_DIR = os.path.join(tmp, "staging")
    alarms = []
    HT.write_alarm_note = lambda text: alarms.append(text) or {"ok": True}

    store = os.path.join(tmp, "sessions", "--zz--", "session-zz-1")
    os.makedirs(store, exist_ok=True)
    sessfile = os.path.join(store, DS.SESSION_SUFFIX)
    iface = FakeInterface(os.path.join(tmp, "spool", "agent.dsh"))
    HT._post_call = iface.call
    HT._CFG.clear()
    HT._CFG.update({"since": "", "stall": 60, "takeover": False})

    src = HS.source_for("agent.dsh", sessions_dir=os.path.join(tmp, "sessions"))
    HT._SOURCES.clear()
    HT._SOURCES["agent.dsh"] = src
    state = {}

    try:
        print("① 连上 agent 接口（agent.attach）——「连上谁就录谁」那一步")
        ok, res = HT.attach_agent(state, "agent.dsh")
        check("attach 成功且回执给了记录入口与 spool", ok
              and res.get("record_tool") == "agent.record.dsh"
              and res.get("spool_dir") == iface.spool_dir, res)
        check("接口那边记得谁挂上了", "plugin.hook-transcript" in iface.attached, iface.attached)

        print("② 首次：源里的记录全部**交给接口**（不是我直接写 spool）")
        write_frames(sessfile, [
            rec("session", 0, DAY1, {"agentPreset": "standard"}, cwd="D:\\zz", version=3),
            rec("permission/preset", 1, DAY1),
            rec("turn/start", 2, DAY1, {"turn": 1}),
            rec("user/message", 3, DAY1, {"source": {"kind": "user"},
                                          "content": [{"type": "text", "text": "帮我看看"}]}),
            rec("assistant/message", 4, DAY1, {"message": {"content": [
                {"type": "reasoning", "text": "思考：先看文件"},
                {"type": "text", "text": "好的。"},
                {"type": "tool-call", "name": "read", "arguments": '{"path":"a.md"}'}]}}),
            rec("tool/result", 5, DAY1, {"message": {"content": [
                {"type": "tool-result", "toolCallId": "c1",
                 "content": [{"type": "text", "text": "X" * 12000}]}]}}),
            rec("user/message", 6, DAY2, {"source": {"kind": "user"},
                                          "content": [{"type": "text", "text": "第二天"}]}),
        ])
        rep = HT.pull_agent(state, "agent.dsh", force=True)
        check("拉到 6 条并交付给接口", rep.get("new") == 6 and iface.batches == 1, rep)
        check("spool 是**接口**落的：1 日 5 条 / 2 日 1 条",
              len(iface.rd("2099-01-01")) == 5 and len(iface.rd("2099-01-02")) == 1,
              (len(iface.rd("2099-01-01")), len(iface.rd("2099-01-02"))))
        ev = iface.rd("2099-01-01")[0]
        check("事件形状与接口 record() 一致", ev["agent"] == "agent.dsh"
              and ev["source"] == "dsh" and set(ev) >= {"type", "time", "payload"}, sorted(ev))
        check("噪声不收（permission）", "permission/preset" not in
              [x["type"] for x in iface.records])
        check("超长工具结果留痕", "共 12000 字" in
              [x for x in iface.records if x["type"] == "tool/result"][0]["payload"]["raw"]["toolResult"])
        check("reasoning 默认不收",
              "思考：先看文件" not in json.dumps(iface.records, ensure_ascii=False))

        print("③ 幂等：源没变就不读（轻轮询只 stat）")
        rep = HT.pull_agent(state, "agent.dsh")
        check("第二次一条不交", rep.get("new") == 0 and rep.get("skipped") == "源没变", rep)

        print("④ 源长了 → 只交新增")
        write_frames(sessfile, [rec("user/message", 7, DAY2, {"source": {"kind": "user"},
                                                              "content": [{"type": "text",
                                                                           "text": "再问"}]})])
        rep = HT.pull_agent(state, "agent.dsh")
        check("只交 1 条", rep.get("new") == 1, rep)
        check("接口那边累计 7 条", len(iface.records) == 7, len(iface.records))

        print("⑤ 偏移从 0 重读（模拟偏移落在帧中间）→ 靠 seq 去重，一条不重复")
        state["sources"]["agent.dsh"]["sessions"] = {
            k: dict(v, offset=0) for k, v in state["sources"]["agent.dsh"]["sessions"].items()}
        rep = HT.pull_agent(state, "agent.dsh", force=True)
        check("重读 0 条新增、接口总数不变", rep.get("new") == 0 and len(iface.records) == 7,
              (rep.get("new"), len(iface.records)))

        print("⑥ 直报型 agent（reasonix）：只挂不采")
        push = HS.source_for("agent.reasonix")
        check("拿到的是 PushOnlySource", isinstance(push, HS.PushOnlySource), type(push).__name__)
        rep = HT.pull_agent({}, "agent.reasonix", force=True)
        check("它这一源不采（日志由 hook 直报接口）", rep.get("new") == 0 and "直报" in rep.get("skipped", ""),
              rep)
        check("也看不出来它在不在用（看门狗别乱下结论）", push.activity_hint() is None)

        print("⑦ 看门狗：源在动、却已经很久没有新日志 → 告警；再来新的 → 解除")
        HT._SOURCES["agent.dsh"] = src
        state["sources"]["agent.dsh"]["last_new_epoch"] = time.time() - 9999
        state.pop("alarm", None)
        rep = HT.check_watchdog(state, time.time())
        check("告警一次并写了告警纸", rep["alarm"] == "agent.dsh" and len(alarms) == 1, rep)
        check("告警纸说清了挂在哪、先看哪几处",
              "转录中断" in alarms[0] and "先看这几处" in alarms[0])
        rep2 = HT.check_watchdog(state, time.time() + 30)
        check("还卡着不重复刷告警", rep2["alarm"] == "" and len(alarms) == 1, rep2)
        write_frames(sessfile, [rec("user/message", 8, DAY2, {"source": {"kind": "user"},
                                                              "content": [{"type": "text",
                                                                           "text": "好了"}]})])
        HT.pull_agent(state, "agent.dsh", force=True)
        check("恢复时改写告警纸", len(alarms) == 2 and "恢复" in alarms[1], alarms[1][:60])

        print("⑧ 接管：老的「推」那条路留下的 spool 挪走（用接口给的 spool_dir，不写死路径）")
        with open(os.path.join(iface.spool_dir, "2099-01-02.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "PreToolUse", "source": "dsh",
                                "time": "2099-01-02T09:00:00"}, ensure_ascii=False) + "\n")
        st2 = dict(state, took_over=False)
        moved = HT.takeover(st2)
        check("挪走了一个文件", any("2099-01-02" in m for m in moved), moved)
        check("接管只做一次", HT.takeover(st2) == [], "第二次返回空")
    finally:
        (HT.STATE_FILE, HT.SPOOL_DIR, HT.STAGING_DIR, HT._post_call,
         HT.write_alarm_note) = real
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 70)
    print("通过" if OK else "失败：%s" % "、".join(FAILED))
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
