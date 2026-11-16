# -*- coding: utf-8 -*-
"""补齐 09-28～10-01 四天的转录缺口（辞玖 2026-10-02）。

  python scripts/backfill_transcripts.py [--apply]

背景：库内 reasonix-raw 停在 **2026-09-27**（搬运工 09-27 停用），而 vault 侧 09-28 起每天都有。
这五天全机**只有 vault 那一份**，而那份是**旧 writer 截断过的**（助手正文 100 字节 / 用户 prompt
150 字节，且不留痕）—— reasonix 自己的会话存储里没有这几天的完整件（那边最新是 08-05）。
所以**如实补、如实标**：文首写清它来自旧 writer、哪些地方被截过；**2026-10-02 20:11 起**的转录
由新链路产生，内容是原文。

为什么 10-02 不补：它还在长（旧 writer 一直在写），而且新链路从今天 20:11 起已接管 ——
补一份"截断的半天"进库只会跟新链路那份打架。要补 10-02 的上午，请辞玖说一声。

默认**干跑**（只打印要做什么），加 --apply 才真写。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = "http://127.0.0.1:8848"
VAULT_RAW = r"D:\个人数据\辞玖\日志\reasonix-raw"
STAGING = os.path.join(ROOT, "data", "transcripts", ".backfill")
DAYS = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"]

MARK = """> ⚠️ **这份转录来自旧 writer（supervisor-v2），内容有截断**：它写盘时把助手正文截到
> **100 字节**、用户 prompt 截到 **150 字节**，而且**不留痕迹**——原样抓不回来了。
> 同机没有更完整的副本（reasonix 自己的会话存储里这几天的件已不存在）。
> **2026-10-02 20:11 起**的转录由新链路（agent 接口 → plugin.transcript）产生，**内容是原文、不截断**。
"""


def call(path, body=None, method="GET", timeout=180):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json", "X-Writer": "transcript-backfill"}
    try:
        with open(os.path.join(ROOT, "data", "plugins", "token.json"), encoding="utf-8") as f:
            h["X-Plugin-Token"] = str(json.load(f).get("token") or "")
    except Exception:
        pass
    req = urllib.request.Request(LIB + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]


def lib_has(day):
    _st, t = call("/api/tree")
    want = "vault/日志/reasonix-raw/" + day
    hit = []

    def walk(n, pre):
        p = (pre + "/" + n.get("name", "")).strip("/") if n.get("name") else pre
        if p == want:
            hit.append(p)
        for c in n.get("children", []):
            walk(c, p)
    walk(t.get("tree") or {}, "")
    return bool(hit)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写（默认只干跑）")
    a = ap.parse_args()

    todo = []
    for day in DAYS:
        src = os.path.join(VAULT_RAW, day, "会话-202103.md")
        if not os.path.isfile(src):
            print("  ⏭ %s：vault 里没有 %s" % (day, src))
            continue
        if lib_has(day):
            print("  ⏭ %s：库里已经有了（幂等，跳过）" % day)
            continue
        todo.append((day, src, os.path.getsize(src)))

    print("\n要补 %d 天：" % len(todo))
    for day, src, size in todo:
        print("   %s  %s（%d 字节）" % (day, src, size))
    if not todo:
        print("没有要补的。")
        return 0
    if not a.apply:
        print("\n（干跑：加 --apply 才真写）")
        return 0

    bad = 0
    for day, src, size in todo:
        stage = os.path.join(STAGING, day)
        os.makedirs(stage, exist_ok=True)
        with open(src, encoding="utf-8", errors="replace") as f:
            body = f.read()
        if not body.lstrip().startswith(">"):
            body = MARK + "\n" + body
        dst = os.path.join(stage, "会话-202103.md")
        with open(dst, "w", encoding="utf-8") as f:
            f.write(body)
        st, r = call("/api/import", {"src": stage, "under": "vault/日志/reasonix-raw/" + day,
                                     "overwrite": True, "rag": False}, "POST")
        ok = st == 200 and isinstance(r, dict) and r.get("ok")
        print("  %s %s → imported=%s updated=%s skipped=%s" %
              ("✅" if ok else "❌", day,
               (r or {}).get("imported"), (r or {}).get("updated"), (r or {}).get("skipped")))
        if not ok:
            bad += 1
            print("     错误：", json.dumps(r, ensure_ascii=False)[:200])

    print("\n复核（库里现在有没有这几天）：")
    for day, _src, _size in todo:
        print("   %s  %s" % (day, "✅ 有了" if lib_has(day) else "❌ 还是没有"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
