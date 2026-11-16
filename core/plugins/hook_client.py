#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用 hook 客户端 —— **任何支持 hook 的 agent 都指到这一个脚本**（别再每个 agent 复制一份）。

用法（agent 侧配置里写这一条命令即可）：

    python D:/core/plugins/hook_client.py --agent claude-code

agent 会从 **stdin 喂一条 JSON** 进来（各家的字段名不一样，这里做宽松映射），本脚本：

    stdin JSON ──▶ 事件 {type, source, time, payload} ──▶ POST /api/bridge/send
                                                        └─→ agent.record.<agent>（薄入口：只落 spool）

三条规矩（照抄 reasonix hook 那套已经验过的做法）：
  1. **限时很短**（默认 1 秒）：hook 在 agent 的回合里跑，宁可下次补送也不许拖住它；
  2. 失败落本地 outbox（`data/transcripts/<agent>/.hook-outbox.jsonl`），**下次调用顺手补送**；
  3. 自己的日志一律进 stderr —— stdout 留给 hook 协议（有的 agent 认 stdout 的返回值）。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
HOST = os.environ.get("SB_HOST", "http://127.0.0.1:8790")
TOKEN_FILE = os.path.join(CORE_DIR, "data", "plugins", "token.json")


def log(msg):
    print("[hook_client] %s" % msg, file=sys.stderr, flush=True)


def token():
    tok = os.environ.get("PLUGIN_TOKEN", "")
    if tok:
        return tok
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return (json.load(f) or {}).get("token", "") or ""
    except Exception:
        return ""


def outbox_path(agent):
    return os.path.join(CORE_DIR, "data", "transcripts", agent, ".hook-outbox.jsonl")


def to_event(agent, raw):
    """各家的 hook JSON 字段名不一样 → 宽松映射成接口认的形状（尽量把原文整条留住）"""
    if not isinstance(raw, dict):
        raw = {"value": raw}
    typ = (raw.get("hook_event_name") or raw.get("event") or raw.get("type")
           or raw.get("hook_event") or "hook")
    sess = (raw.get("session_id") or raw.get("sessionId") or raw.get("conversation_id")
            or raw.get("thread_id") or "")
    return {"type": str(typ), "source": agent,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "payload": {"session": str(sess), "raw": raw}}


def send(agent, ev, timeout=1.0):
    body = json.dumps({"id": "hook-%d" % int(time.time() * 1000),
                       "agentId": agent, "kind": "tool-call",
                       "call": {"tool": "agent.record." + agent.rsplit(".", 1)[-1],
                                "args": {"event": ev}}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(HOST + "/api/bridge/send", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Plugin-Token": token()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "replace") or "{}")
    return bool(d.get("ok") or (d.get("result") or {}).get("ok"))


def flush_outbox(agent, budget=0.8):
    """上次没送出去的，这次顺手补（限时；失败就留着）"""
    p = outbox_path(agent)
    if not os.path.isfile(p):
        return 0
    try:
        with open(p, encoding="utf-8") as f:
            lines = [x for x in f.read().split("\n") if x.strip()]
        rest, sent = [], 0
        t0 = time.time()
        for i, line in enumerate(lines):
            if time.time() - t0 > budget:
                rest.extend(lines[i:])
                break
            try:
                if send(agent, json.loads(line), timeout=0.5):
                    sent += 1
                else:
                    rest.append(line)
            except Exception:
                rest.append(line)
        with open(p + ".tmp", "w", encoding="utf-8", newline="\n") as f:
            for line in rest:
                f.write(line + "\n")
        os.replace(p + ".tmp", p)
        return sent
    except Exception as e:
        log("补送失败（忽略）：%s" % e)
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True, help="agent 短名（对应 agents/<id>.yaml）")
    ap.add_argument("--timeout", type=float, default=1.0, help="上报限时（秒）")
    a = ap.parse_args()

    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {"raw_text": raw[:4000]}
    ev = to_event(a.agent, payload)
    try:
        flush_outbox(a.agent)
        ok = send(a.agent, ev, timeout=a.timeout)
    except Exception as e:
        ok = False
        log("上报失败（落 outbox，下次补）：%s: %s" % (type(e).__name__, str(e)[:80]))
    if not ok:
        try:
            p = outbox_path(a.agent)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception as e:
            log("outbox 也写不了（只能丢这条）：%s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
