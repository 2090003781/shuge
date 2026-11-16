# -*- coding: utf-8 -*-
"""给 `sync_preset_identity.py` 出「库优先 + 锁检查」的改法（**只出方案 + 验证，不提交**）。

为什么必须改（干跑已确证）：
  它是人设进 DSH 预设的**唯一通道**（`config.yaml:199-200` 写明这条链）。
  现在它 `open(VAULT/PERSONA_REL)` 直接读旧 vault 磁盘 ——
  人设迁进私密文件夹后旧路径没了 → 脚本读不到 → **人设同步断掉**。
  顺带修掉已确证的隐患：它读的是**退役旧 vault 的明文副本**，锁形同虚设。

本脚本只做三件事：
  A. 生成改好的文件到 `sync_preset_identity.library_first.py`（**不覆盖原文**）
  B. 在真库上验证「库优先」这条路真能读到人设
  C. 打印改动摘要
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"D:\core\plugins\sync_preset_identity.py"
DST = r"D:\core\plugins\sync_preset_identity.library_first.py"
API = "http://127.0.0.1:8848"
PERSONA_REL = "second-brain-system/dsh/DSH-人设.md"
DISK_VAULT = r"D:\个人数据\辞玖"


class PersonaUnavailable(Exception):
    """人设读不到 —— 不静默、不拿旧的凑"""


def lib_persona():
    """问库取人设（GET /api/content + path 查询参数）→ (text|None, 来源说明)"""
    q = urllib.parse.quote("vault/" + PERSONA_REL.replace(os.sep, "/"))
    with urllib.request.urlopen(API + "/api/content?max=1000000&path=" + q, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8"))
    it = d.get("item") or {}
    if it.get("locked"):
        raise PersonaUnavailable("人设在库里已加密上锁（先在库里解锁再跑）")
    if not it.get("found"):
        return None, "库里没有这一篇"
    return it.get("content") or "", "库"


def clean_identity(raw: str) -> str:
    """与目标脚本同款的清洗规则（去 frontmatter / 整行 HTML 注释 / 引用行）"""
    no_fm = re.sub(r"^---\r?\n[\s\S]*?\r?\n---\r?\n?", "", raw)
    fence = False
    out = []
    for line in re.split(r"\r?\n", no_fm):
        if re.match(r"^\s*```", line):
            fence = not fence
        if fence or (not re.match(r"^\s*<!--", line) and not re.match(r"^\s*>", line)):
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


HDR_OLD = (
    'VAULT = os.environ.get("VAULT_PATH", r"D:\\个人数据\\辞玖")\n'
    'PERSONA_REL = os.path.join("second-brain-system", "dsh", "DSH-人设.md")\n'
)

HDR_NEW = '''VAULT = os.environ.get("VAULT_PATH", r"D:\\个人数据\\辞玖")
PERSONA_REL = os.path.join("second-brain-system", "dsh", "DSH-人设.md")

# 库（真相源）地址。人设归库，且**可能是私密篇** —— 读它要走库，不走磁盘。
#   2026-10-15 两个原因：
#     ① 原来直接 open(VAULT/PERSONA_REL) 读到的是**退役 vault 的明文副本**（锁形同虚设）
#     ② 人设迁进私密文件夹后旧路径消失 → 读不到 → 人设同步断掉
LIBRARY_URL = os.environ.get("LIBRARY_URL", "http://127.0.0.1:8848").rstrip("/")


class PersonaUnavailable(Exception):
    """人设读不到（库锁着 / 库里没有 / 库与磁盘都没有）—— 不静默、不拿旧的凑"""


def _from_library():
    """先问库（真相源）。返回 (text|None, 说明)。

    库回 locked → 抛 PersonaUnavailable：**绝不退回磁盘**。
    拿旧 vault 的明文冒充"当前人设"，比读不到更糟（静默的错比响的错危险）。
    """
    import json as _json
    import urllib.parse as _up
    import urllib.request as _u
    _q = _up.quote("vault/" + PERSONA_REL.replace(os.sep, "/"))
    with _u.urlopen(LIBRARY_URL + "/api/content?max=1000000&path=" + _q, timeout=30) as r:
        d = _json.loads(r.read().decode("utf-8"))
    it = d.get("item") or {}
    if it.get("locked"):
        raise PersonaUnavailable(
            "人设在库里已加密上锁 —— 先在库里解锁（搜索框输库密码）再跑本脚本。"
            "不拿磁盘上那份旧明文凑数。")
    if not it.get("found"):
        return None, "库里没有这一篇"
    return it.get("content") or "", "库"
'''

FUNC_OLD = '''def persona_text():
    p = os.path.join(VAULT, PERSONA_REL)
    with io.open(p, encoding="utf-8") as f:
        return clean_identity(f.read())
'''

FUNC_NEW = '''def persona_text():
    """人设正文：**先问库（真相源）**，库不可达才退磁盘并警告。"""
    try:
        txt, _why = _from_library()
    except PersonaUnavailable:
        raise
    except Exception as e:
        print("[identity] 库不可达（%s）—— 退回磁盘读退役副本，请注意这可能是旧版"
              % str(e)[:60], file=sys.stderr)
        txt = None
    if txt is not None:
        return clean_identity(txt)
    p = os.path.join(VAULT, PERSONA_REL)
    if not os.path.isfile(p):
        raise PersonaUnavailable("磁盘也没有：%s（人设可能已迁进私密文件夹，请先解锁库）" % p)
    with io.open(p, encoding="utf-8") as f:
        return clean_identity(f.read())
'''


def main() -> int:
    print("=" * 78)
    print("sync_preset_identity.py：库优先改法（方案 + 验证，不提交）")
    print("=" * 78)

    src = io.open(SRC, encoding="utf-8").read()

    if HDR_OLD not in src:
        print("  A. ✗ 头部锚点未匹配（脚本可能已改过）")
        return 1
    if FUNC_OLD not in src:
        print("  A. ✗ persona_text 锚点未匹配")
        return 1
    out = src.replace(HDR_OLD, HDR_NEW, 1).replace(FUNC_OLD, FUNC_NEW, 1)
    io.open(DST, "w", encoding="utf-8", newline="").write(out)
    print("  A. 已生成：%s（原文件未动）" % DST)
    try:
        import py_compile
        py_compile.compile(DST, doraise=True)
        print("     语法检查 OK")
    except Exception as e:
        print("     ✗ 语法检查失败：%s" % str(e)[:80])
        return 1

    print()
    print("  B. 在真库上验证「库优先」可用")
    try:
        txt, why = lib_persona()
        if txt is None:
            print("     ✗ 库里没有这一篇（%s）" % why)
            return 1
        cleaned = clean_identity(txt)
        print("     库读回 %d 字符 → 清洗后 %d 字符（来源：%s）" % (len(txt), len(cleaned), why))
        print("     清洗后首行：%s" % cleaned.split("\n")[0][:64])
        disk_p = os.path.join(DISK_VAULT, PERSONA_REL)
        if os.path.isfile(disk_p):
            d_txt = clean_identity(io.open(disk_p, encoding="utf-8").read())
            print("     磁盘那份 %d 字符 —— %s"
                  % (len(d_txt), "与库一致" if d_txt == cleaned else "**与库不同**（磁盘是退役旧版，正是该避免的）"))
        print("     ★ 库优先这条路可用")
    except PersonaUnavailable as e:
        print("     ! 库锁着：%s" % e)
        print("       （这恰好说明改法正确：锁着时明确报错，而不是拿旧明文凑）")
    except Exception as e:
        print("     ✗ 失败：%s: %s" % (type(e).__name__, str(e)[:70]))
        return 1

    print()
    print("  C. 改动摘要")
    print("     + LIBRARY_URL / PersonaUnavailable / _from_library()")
    print("     ~ persona_text()：库优先 → 锁着报错 → 才退磁盘（退时写 stderr 警告）")
    print("     原文件  ：%s（**未改**）" % SRC)
    print("     改好版  ：%s" % DST)
    print()
    print("=" * 78)
    print("★ 方案已备好并验证；**未提交**。落地需你确认（它决定人设内容的来源路径）。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
