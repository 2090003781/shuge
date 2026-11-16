# -*- coding: utf-8 -*-
"""验收「第四路」：reasonix hook 真会把事件发进平台（辞玖 2026-10-02）。

  python scripts/smoke_hook_uplink.py

它驱动的是**平台外**那个文件（`~/.reasonix/logs/hook_logger.py`），走的是 reasonix 调它的同一条路：
把 JSON 从 **stdin** 喂进去。验四件事：

  ① **真的发出去了**：事件到了 agent.reasonix 接口的 spool（data/transcripts/agent.reasonix/<今天>.jsonl）
  ② **没串**：没进 agent.dsh 的 spool
  ③ **不占预算**：整个 hook 进程的墙钟 < 3 秒（它的总预算是 5 秒，超了 reasonix 报 warn/block）
  ④ **送的是原文**：spool 里带得回完整内容（不是被 100/150 字节砍过的那份）

自清：跑完把这次测试写进 spool 的那几行**按标记删掉**（并确认没有因此把测试内容留在库里）。
找不到 hook 文件时**跳过**（不报错）—— 这个脚本在别的机器上没意义。
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(os.path.expanduser("~"), ".reasonix", "logs", "hook_logger.py")
BACKUP = os.path.join(os.path.expanduser("~"), ".reasonix", "logs",
                      "hook_logger.py.bak-20261002-before-uplink")
SPOOL = os.path.join(ROOT, "data", "transcripts")
DAY = time.strftime("%Y-%m-%d")
MARK = "ZZ-UPLINK-%d" % int(time.time())
OK, BAD, SKIP = [], [], []


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print("  %s %s%s" % ("✅" if cond else "❌", name, ("  | " + str(extra)[:180]) if extra else ""))


def skip(name, why=""):
    SKIP.append(name)
    print("  ⏭ %s%s" % (name, ("  | " + str(why)[:180]) if why else ""))


def spool(agent):
    return os.path.join(SPOOL, agent, DAY + ".jsonl")


def lines_of(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return [x for x in f if x.strip()]
    except Exception:
        return []


def drop_marked(p, mark):
    """把带 mark 的行删掉（自清）"""
    ls = lines_of(p)
    keep = [x for x in ls if mark not in x]
    if len(keep) == len(ls):
        return 0
    if keep:
        with open(p, "w", encoding="utf-8") as f:
            f.writelines(keep)
    else:
        try:
            os.remove(p)
        except Exception:
            pass
    return len(ls) - len(keep)


if not os.path.isfile(HOOK):
    skip("reasonix hook 不在本机", HOOK)
    print("\n通过 0 / 0；跳过 %d；失败：无" % len(SKIP))
    sys.exit(0)

before = len(lines_of(spool("agent.reasonix")))
before_other = len(lines_of(spool("agent.dsh")))
print("hook：%s" % HOOK)
print("发一条带标记的事件（标记 %s）……" % MARK)

payload = json.dumps({"event": "PostToolUse", "tool": "Read",
                      "prompt": "zz 第四路验收",
                      "tool_response": {"note": MARK, "detail": "这是一段用来验「原文完整」的内容。" * 8}},
                     ensure_ascii=False)
t0 = time.time()
try:
    p = subprocess.run([sys.executable, HOOK], input=payload.encode("utf-8"),
                       capture_output=True, timeout=30,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    rc, err = p.returncode, (p.stderr or b"").decode("utf-8", "replace")[-300:]
except subprocess.TimeoutExpired:
    rc, err = -1, "超时"
dt = time.time() - t0

try:
    chk("hook 进程正常退出（没被我们的改动弄崩）", rc == 0, "rc=%s stderr=%s" % (rc, err[:160]))
    chk("hook 墙钟 < 3 秒（总预算 5 秒，留足余量）", dt < 3.0, "%.2fs" % dt)

    after = lines_of(spool("agent.reasonix"))
    chk("事件进了 agent.reasonix 的 spool", len(after) > before,
        "%d → %d 行" % (before, len(after)))
    hit = [x for x in after if MARK in x]
    chk("进的就是这一条（标记对得上）", bool(hit), (hit[0][:140] if hit else "没找到"))
    if hit:
        ev = json.loads(hit[0])
        raw = json.dumps((ev.get("payload") or {}).get("raw") or {}, ensure_ascii=False)
        chk("送的是**原文**（内容完整、没被砍）", MARK in raw and "原文完整" in raw,
            "raw %d 字节" % len(raw.encode("utf-8")))
        chk("事件类型原样带过来", ev.get("type") == "PostToolUse", ev.get("type"))

    after_other = len(lines_of(spool("agent.dsh")))
    chk("没串到 agent.dsh 的 spool", after_other == before_other,
        "%d → %d 行" % (before_other, after_other))
    print("\n② 回归：注入那条路没被插代码弄坏（UserPromptSubmit）")
    MARK2 = MARK + "-INJECT"
    payload2 = json.dumps({"event": "UserPromptSubmit", "prompt": "zz 注入回归 " + MARK2},
                          ensure_ascii=False)
    t0 = time.time()
    try:
        p2 = subprocess.run([sys.executable, HOOK], input=payload2.encode("utf-8"),
                            capture_output=True, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        rc2 = p2.returncode
        out2 = (p2.stdout or b"").decode("utf-8", "replace")
        err2 = (p2.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        rc2, out2, err2 = -1, "", "超时"
    dt2 = time.time() - t0
    chk("UserPromptSubmit 也不崩（rc=0，stderr 空）", rc2 == 0 and not err2.strip(),
        "rc=%s stderr=%s" % (rc2, err2[-160:]))
    chk("墙钟仍 < 3 秒", dt2 < 3.0, "%.2fs" % dt2)
    after2 = lines_of(spool("agent.reasonix"))
    chk("这条也进了 spool（UserPromptSubmit 类型也走通）",
        any(MARK2 in x for x in after2), "%d 行" % len(after2))

    # ── 真正能判"改坏没改坏"的判据：跟改动前那份 hook 比，同输入 → 同输出 ──
    # （"stdout 非空"是错的判据：注入只在 SessionStart 取包，UserPromptSubmit 本来就可能是空的）
    if os.path.isfile(BACKUP):
        print("\n③ 跟改动前对照（%s）" % os.path.basename(BACKUP))
        for evname, pl in (("UserPromptSubmit", payload2),
                           ("PostToolUse", payload + " ")):     # 加个空格避免与上一轮完全同字节
            res = {}
            for tag, script in (("旧", BACKUP), ("新", HOOK)):
                try:
                    pp = subprocess.run([sys.executable, script], input=pl.encode("utf-8"),
                                        capture_output=True, timeout=30,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    res[tag] = (pp.returncode, pp.stdout or b"", pp.stderr or b"")
                except subprocess.TimeoutExpired:
                    res[tag] = (-1, b"", b"timeout")
            same_rc = res["旧"][0] == res["新"][0]
            same_out = res["旧"][1] == res["新"][1]
            chk("%s：rc 与改动前一致" % evname, same_rc, "%s vs %s" % (res["旧"][0], res["新"][0]))
            chk("%s：**stdout 逐字节一致**（既有行为没被我改动）" % evname, same_out,
                "旧 %d 字节 / 新 %d 字节" % (len(res["旧"][1]), len(res["新"][1])))
    else:
        skip("跟改动前对照", "没找到 %s（放一份改动前的 hook 才能做这个对照）" % os.path.basename(BACKUP))
finally:
    print("\n清场")
    n1 = drop_marked(spool("agent.reasonix"), MARK)
    n1 += drop_marked(spool("agent.reasonix"), MARK + "-INJECT")
    n2 = drop_marked(spool("agent.dsh"), MARK)
    print("   从 spool 里删掉测试行：reasonix %d 行 / dsh %d 行" % (n1, n2))
    # 万一插件已经把今天这份收进库了，库里那份也得把测试行洗掉（force 重收一遍）
    try:
        import urllib.request
        tok = ""
        try:
            with open(os.path.join(ROOT, "data", "plugins", "token.json"), encoding="utf-8") as f:
                tok = str(json.load(f).get("token") or "")
        except Exception:
            pass
        body = json.dumps({"plugin": "plugin.transcript", "tool": "transcript.flush",
                           "args": {"agent": "agent.reasonix", "day": DAY, "force": True},
                           "timeoutMs": 60000}).encode("utf-8")
        req = urllib.request.Request("http://127.0.0.1:8790/api/bridge/plugins/call", data=body,
                                     method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-Plugin-Token": tok})
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.loads(r.read().decode("utf-8"))
        print("   顺手按洗干净的 spool 重收一次今天：", json.dumps((d or {}).get("data"), ensure_ascii=False)[:160])
    except Exception as e:
        print("   重收失败（忽略）：", str(e)[:120])

print("\n通过 %d / %d；跳过 %d；失败：%s" % (len(OK), len(OK) + len(BAD), len(SKIP),
                                          "、".join(BAD) if BAD else "无"))
sys.exit(1 if BAD else 0)
