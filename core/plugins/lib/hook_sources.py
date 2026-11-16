# -*- coding: utf-8 -*-
"""钩子转录服务的**源适配器**层：一个 agent 一份读法，服务本身与 agent 无关。

辞玖 2026-10-04 定的形状（方案 A）：**钩子转录服务是内核插件**（内核监督保活），
它**连上某个 agent 接口**之后就负责那个 agent 的日志转录 —— 采集在它这里，
"落到哪、算谁的名下"归 agent 接口（`agent.record.<agent>`）。

    适配器（本件）──事件──▶ agent 接口（agent.dsh / agent.reasonix …）──spool──▶ plugin.transcript ──▶ 库

所以本件只干一件事：**把某个 agent 的日志源读成事件**。约定三个小方法：

    agent_id               这一源服务谁
    signature()            便宜的新鲜度指纹（只 stat）：没变就别去读
    poll(state)            读新记录 → 事件列表（已按接口 record() 的形状备好）
    activity_hint()        这个 agent"看起来在不在用"（看门狗判"源在动却没录上"只靠它）

已有的两个：
    · `DshSessionSource`（agent.dsh）—— DSH 把完整会话写在 `~/.dsh/sessions/**/session.v3.jsonl.zstd`
      （一条记录一个 zstd 帧、只追加、带自增 seq），按"字节偏移 + seq 去重"读。
      为什么是读它而不是等 DSH 推：推的那半条住在 DSH 进程里，内核既看不见死活也没有基准
      （2026-10-03 12:30 静默死了 11 小时没人知道）。
    · `PushOnlySource`（agent.reasonix 这类）—— 事件由 hook **直接上报接口**，服务这边只需要
      "挂着 + 盯静默"，不用采。
"""
import os
import time

from dsh_session import (DEFAULT_SESSIONS_DIR, day_of, decode_from, find_sessions,  # noqa: F401
                         rec_ms, to_event)


class Source:
    """适配器契约（缺方法就报错，别静默降级）"""
    agent_id = ""
    kind = ""

    def signature(self):
        raise NotImplementedError

    def poll(self, state):
        raise NotImplementedError

    def activity_hint(self):
        return None            # None = "我看不出来它在不在用"（看门狗就别下结论）


class DshSessionSource(Source):
    """agent.dsh：读 DSH 自己的会话存储（不依赖 DSH 进程里的任何东西）"""

    agent_id = "agent.dsh"
    kind = "file"

    def __init__(self, sessions_dir=None, since="", with_reasoning=False,
                 tool_result_max=8000, tool_args_max=2000, active_window=600):
        self.sessions_dir = sessions_dir or DEFAULT_SESSIONS_DIR
        self.since = since
        self.with_reasoning = with_reasoning
        self.tool_result_max = tool_result_max
        self.tool_args_max = tool_args_max
        self.active_window = active_window

    def sessions(self):
        return find_sessions(self.sessions_dir)

    def signature(self):
        """只 stat：{(会话 id): (size, mtime 取整)}。变了才值得去读。"""
        return {s["id"]: (s["size"], int(s["mtime"])) for s in self.sessions()}

    def activity_hint(self):
        """10 分钟内有会话文件在长 → "DSH 在用"（看门狗据此判"源在动却没录上"）"""
        now = time.time()
        return any(now - s["mtime"] < self.active_window for s in self.sessions()) or None

    def poll(self, state):
        """把新记录读成事件。返回 (events, report)。

        `state` 就放本源的进度：`{"sessions": {会话id: {offset, last_seq, …}}}`。
        偏移（少读） + `seq` 去重（读重了也不重复）—— 两条一起用才不重不漏。
        """
        st = state.setdefault("sessions", {})
        events, rep = [], {"new": 0, "errors": [], "reset_reads": 0, "days": {}}
        for s in self.sessions():
            key = s["id"]
            prev = st.get(key) or {}
            offset = int(prev.get("offset") or 0)
            last_seq = prev.get("last_seq")
            if offset and s["size"] < offset:          # 文件被换过/截断 → 从头
                offset, last_seq = 0, None
            if offset == s["size"] and last_seq is not None:
                continue                                # 没长，跳过（省事）
            recs, after, err = decode_from(s["path"], offset)
            if err:
                rep["reset_reads"] += 1
                recs, after, err2 = decode_from(s["path"], 0)
                if err2:
                    rep["errors"].append("%s: %s" % (key, err2))
                    continue
                last_seq = None
            newest = last_seq
            added = 0
            for rec in recs:
                seq = rec.get("seq")
                if last_seq is not None and isinstance(seq, int) and seq <= last_seq:
                    continue                            # 去重
                ev = (to_event(rec, session_id=key, cwd=s["cwd"],
                               with_reasoning=self.with_reasoning,
                               tool_result_max=self.tool_result_max,
                               tool_args_max=self.tool_args_max)
                      if self._in_window(rec) else None)
                if ev is None:
                    if isinstance(seq, int):
                        newest = seq if newest is None else max(newest, seq)
                    continue
                events.append(ev)
                d = str(ev.get("time") or "")[:10]
                rep["days"][d] = rep["days"].get(d, 0) + 1
                added += 1
                if isinstance(seq, int):
                    newest = seq if newest is None else max(newest, seq)
            st[key] = {"path": s["path"], "offset": max(after, offset), "last_seq": newest,
                       "mtime": s["mtime"], "size": s["size"],
                       "added": int(prev.get("added") or 0) + added}
            rep["new"] += added
        return events, rep

    def _in_window(self, rec):
        """`since` 只管首次：早于它的记录不读（状态里记着进度，之后不会再问它）"""
        if not self.since:
            return True
        return day_of(rec_ms(rec)) >= self.since


class PushOnlySource(Source):
    """agent.reasonix 这类：事件由 hook 直报接口 —— 服务只挂着、盯静默，不采也不猜"""

    kind = "push"

    def __init__(self, agent_id):
        self.agent_id = agent_id

    def signature(self):
        return None            # 没有可 stat 的源；不参与"变了才读"的判断

    def poll(self, state):
        return [], {"new": 0, "errors": [], "reset_reads": 0, "days": {}, "push_only": True}

    def activity_hint(self):
        return None            # 看不出来（reasonix 在不在跑，归它自己报）


# 注册表：想接新 agent，在这儿加一行（或从外部注册一个 Source 子类）
SOURCES = {"agent.dsh": DshSessionSource}
PUSH_ONLY = {"agent.reasonix"}          # 事件由 hook 直报接口，没有可读的源


def source_for(agent_id, **kw):
    """给 agent id 找一个源；没有源但属于"直报型" → PushOnlySource；都没有 → None"""
    cls = SOURCES.get(agent_id)
    if cls is not None:
        return cls(**kw)
    if agent_id in PUSH_ONLY:
        return PushOnlySource(agent_id)
    return None


def known_agents():
    return sorted(list(SOURCES) + sorted(PUSH_ONLY))
