# -*- coding: utf-8 -*-
"""注入包（context-pack）验收：**路由文档保存要认「只在库里」的条目**（辞玖 2026-10-01 15:50 报）。

  python scripts/smoke_contextpack.py

### 修的是什么（先复现、再对照，最后才改）

辞玖报的原文：往 `agent.dsh` 的路由文档里加 `vault/个人/画像/用户画像-核心.md` →
弹「保存路由文档失败: ValueError: 这些条目不可用：vault/个人/画像/用户画像-核心.md （不可用）」。

根因：同一个路径**两套口径** ——
  · 命名包那条路（`pack.create` / `pack.update`）走 `validate_files` → `entry_exists`（**库优先、磁盘兜底**）；
  · 路由文档那条路（`pack.entries`）只调 `resolve_path`（**只查磁盘**，还把 `vault/` 前缀当 vault 相对路径）。
于是「只在库里、磁盘上没有」的条目在路由文档里被拒、在命名包里却收。
更糟的是文案：报错时又回头用 `entry_exists` 复查（这次问库，说它有）→ err 为空 → 显示成「（不可用）」，
**既拒了人，又不给任何理由**。

### 本脚本怎么验

自建自清，**不碰主人任何一张真实卡片**：用 scratch agent `zz-ctx-smoke` 写路由文档，验完 reset；
夹具是库里现场建的一篇 `zz-` 笔记（库里专有），验完删除；收尾比对 `routes-docs.json` 逻辑内容没变。
"""
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "http://127.0.0.1:8790"
NOTES = "http://127.0.0.1:8848"
AGENT = "zz-ctx-smoke"                     # scratch：不碰 agent.dsh / agent.reasonix
NAME = "zz-注入包-库里专有.md"
LIB_PATH = "vault/" + NAME                  # 库内路径（vault/ 前缀 = 库的顶层文件夹名）
VAULT = os.environ.get("VAULT_PATH", r"D:\个人数据\辞玖")

OK, BAD = [], []
FID = None


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print("  %s %s%s" % ("✅" if cond else "❌", name, ("  | " + str(extra)[:170]) if extra else ""))


def call(url, body=None, method="GET", timeout=90):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]


def ctx(tool, args):
    """走插件的正式工具通道（跟控制台点按钮同一条路）"""
    _st, r = call(CORE + "/api/bridge/plugins/call",
                  {"plugin": "plugin.context-pack", "tool": tool, "args": args or {},
                   "timeoutMs": 40000}, "POST")
    return r if isinstance(r, dict) else {"ok": False, "error": str(r)[:200]}


def routes_file():
    return os.path.join(ROOT, "data", "context", "routes-docs.json")


def routes_json():
    try:
        with open(routes_file(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


before_json = routes_json()
before_counts = None

try:
    print("⓪ 前置")
    st, h = call(NOTES + "/api/health")
    chk("库在跑（注入包的内容源）", st == 200 and h.get("ok"), st)
    before_counts = h["stats"]["files"]
    r = ctx("pack.list", {})
    chk("插件在跑（pack.list 有回执）", r.get("ok") is True, str(r.get("error"))[:120])

    print("\n① 造一篇「只在库里、磁盘上没有」的夹具")
    _st, fl = call(NOTES + "/api/folders")
    vault = next((f for f in (fl.get("folders") or []) if f.get("path") == "vault"), None)
    chk("库里有顶层文件夹 vault", bool(vault), (fl.get("folders") or [])[:2])
    _st, d = call(NOTES + "/api/files",
                  {"folder_id": vault["id"], "name": NAME,
                   "content": "# 只在库里的一篇\n\nZZCTXONLY 这段文字磁盘上不存在。\n"}, "POST")
    FID = d.get("file_id")
    chk("已入库", bool(FID), d)
    disk = os.path.join(VAULT, "vault", NAME)
    chk("磁盘上确实没有同名文件（这就是要验的场景）", not os.path.isfile(disk), disk)

    print("\n② 老问题这条路：把「只在库里」的条目存进**路由文档**")
    entries = [{"path": LIB_PATH, "enabled": True}]
    r = ctx("pack.entries", {"agent": AGENT, "files": entries})
    chk("现在能存进去（改前：ValueError「（不可用）」）", r.get("ok") is True, str(r.get("error"))[:170])
    back = ctx("pack.entries", {"agent": AGENT})
    got = [e.get("path") for e in ((back.get("data") or {}).get("files") or [])]
    chk("读回来就是这一条", got == [LIB_PATH], got)

    print("\n③ 组包真能取到它（库里的正文进包）")
    pv = ctx("pack.preview", {"files": [LIB_PATH]})
    head = json.dumps(pv.get("data"), ensure_ascii=False)
    chk("取到正文（ZZCTXONLY）", pv.get("ok") is True and "ZZCTXONLY" in head, head[:170])
    pv2 = ctx("pack.preview", {"files": [NAME]})       # 老写法（不带 vault/ 前缀）
    chk("老写法也认（不带 vault/ 前缀）", "ZZCTXONLY" in json.dumps(pv2.get("data"), ensure_ascii=False),
        json.dumps(pv2.get("data"), ensure_ascii=False)[:150])

    print("\n④ 该拒的照旧拒，而且**带具体原因**")
    r = ctx("pack.entries", {"agent": AGENT, "files": entries + [{"path": "vault/zz-根本没这个.md"}]})
    err = str(r.get("error") or "")
    chk("不存在的条目不收", r.get("ok") is False and "zz-根本没这个" in err, err[:180])
    chk("原因不再是干巴巴的「不可用」", "（不可用）" not in err and ("文件不存在" in err or "找不到" in err), err[:180])
    r = ctx("pack.entries", {"agent": AGENT, "files": entries + [{"path": "../../逃逸.md"}]})
    chk("路径穿越照旧被拒", r.get("ok") is False and "非法路径" in str(r.get("error")), str(r.get("error"))[:160])
    st, _ = call(NOTES + "/api/health")
    r = ctx("pack.entries", {"agent": AGENT, "files": entries + [{"path": "C:/Windows/win.ini"}]})
    chk("外部绝对路径（存在）照旧收 —— 老能力没坏", r.get("ok") is True, str(r.get("error"))[:160])
    r = ctx("pack.entries", {"agent": AGENT, "files": entries + [{"path": "C:/Windows/zz-不存在的.ini"}]})
    chk("外部绝对路径（不存在）不收", r.get("ok") is False, str(r.get("error"))[:160])

    print("\n⑤ 命名包那条路没被带坏（它本来就是库优先）")
    r = ctx("pack.create", {"name": "zz-对照包", "files": entries})
    chk("pack.create 收同一个条目", r.get("ok") is True, str(r.get("error"))[:160])
    pid = ((r.get("data") or {}).get("pack") or {}).get("id")
    if pid:
        chk("能删掉", ctx("pack.delete", {"id": pid}).get("ok") is True)

    print("\n⑤b 关键字预览要走映射（2026-10-03 修的：原来三个关键字全报「文件不存在」）")
    # 组装包那条路一直是映射过的（read_doc）；**预览**这条原来漏了 → 截图里 rules-l1 显示
    # 「0 字符 | 读取失败：文件不存在」，而库里其实有的。
    for kw in ("rules-l1", "profile", "persona"):
        r = ctx("pack.preview", {"files": [kw]})
        fl = ((r.get("data") or {}).get("files") or [{}])[0]
        err = str(fl.get("err") or "")
        ok_or_locked = bool(fl.get("ok")) or ("已加密上锁" in err)
        chk("关键字 %-9s 预览不再报「文件不存在」" % kw, ok_or_locked,
            "ok=%s chars=%s resolved=%s err=%s" % (fl.get("ok"), fl.get("chars"),
                                                   fl.get("resolved"), err[:60]))
        if kw == "rules-l1":
            chk("rules-l1 预览拿到内容（1 级摘要）", bool(fl.get("ok")) and (fl.get("chars") or 0) > 0,
                "chars=%s" % fl.get("chars"))
            chk("并且告诉了映射到哪一篇", bool(fl.get("resolved")), fl.get("resolved"))

    print("\n⑥ 控制台发出去的 app.js 就是磁盘上这份（前端改动刷新即生效，不用重编）")
    try:
        with urllib.request.urlopen(CORE + "/core/app.js", timeout=30) as resp:
            served = resp.read()
        with open(os.path.join(ROOT, "web", "app.js"), "rb") as f:
            ondisk = f.read()
        chk("/core/app.js 与 web/app.js 逐字节一致", served == ondisk,
            "%d vs %d 字节" % (len(served), len(ondisk)))
        chk("发出去的版本带「默认收起」这一刀", b"__vaultOpen = { '': true }" in served)
    except Exception as e:
        chk("取 /core/app.js", False, e)
finally:
    print("\n⑦ 清场")
    ctx("pack.entries", {"agent": AGENT, "reset": True})       # 删掉 scratch agent 的覆盖
    if FID:
        call(NOTES + "/api/files/" + FID, None, "DELETE")
    after_json = routes_json()
    scratch_gone = AGENT not in after_json
    chk("scratch agent 的覆盖已清掉", scratch_gone, sorted(after_json.keys()))
    chk("routes-docs.json 的逻辑内容跟开跑前一样（没动主人的卡片）",
        {k: v for k, v in after_json.items() if k != "_defaults"}
        == {k: v for k, v in before_json.items() if k != "_defaults"},
        {"before": before_json, "after": after_json})
    try:
        _st, h = call(NOTES + "/api/health")
        chk("夹具已删除（篇数回到开跑前）", h["stats"]["files"] == before_counts,
            "%s -> %s" % (before_counts, h["stats"]["files"]))
    except Exception as e:
        chk("库已恢复", False, e)

print("\n通过 %d / %d；失败：%s" % (len(OK), len(OK) + len(BAD), "、".join(BAD) if BAD else "无"))
sys.exit(1 if BAD else 0)
