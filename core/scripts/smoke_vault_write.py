"""vault_write 自测：merge 不丢旧内容、append 纯追加、replace 缩短被拒、前像与账本都落。

  python scripts/smoke_vault_write.py
只用临时目录当 vault，不碰真 vault。
"""

import os as _os, sys as _sys          # 门禁用（2026-09-30 体检）

if "--yes" not in _sys.argv and _os.environ.get("COREHOST_ALLOW_DESTRUCTIVE") != "1":
    print(__doc__ or "")
    print("\n【门禁】%s 会真的动到**运行中的枢阁 / 库文件夹**（这个脚本忽略参数，--help 也照跑）。"
          % _os.path.basename(__file__))
    print("       确认要跑：python %s --yes（或设 COREHOST_ALLOW_DESTRUCTIVE=1）"
          % _os.path.basename(__file__))
    _sys.exit(2)
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "vault_write.py")
OK = True


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:160] if extra else ""))


def run(vault, *args, stdin=None):
    p = subprocess.run([sys.executable, SCRIPT, "--vault", vault] + list(args),
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       input=stdin, creationflags=NO_WINDOW)
    try:
        return p.returncode, json.loads((p.stdout or "{}").strip().splitlines()[-1])
    except Exception:
        return p.returncode, {"raw": (p.stdout or "") + (p.stderr or "")}


vault = tempfile.mkdtemp(prefix="vw-")
try:
    f = os.path.join(vault, "记忆", "热缓存.md")
    os.makedirs(os.path.dirname(f), exist_ok=True)
    io.open(f, "w", encoding="utf-8").write("# 热缓存\n\n- 旧条目甲\n- 旧条目乙\n")

    print("① merge：只追加旧文件里没有的行，旧的必须还在")
    rc, d = run(vault, "记忆/热缓存.md", "--writer", "task:日报", "--mode", "merge",
                "--text", "- 旧条目甲\n- 新条目丙\n")
    body = io.open(f, encoding="utf-8").read()
    check("退出 0 且 ok", rc == 0 and d.get("ok"), d)
    check("旧条目都在", "- 旧条目甲" in body and "- 旧条目乙" in body, body)
    check("新条目加上了", "- 新条目丙" in body, body)
    check("重复行没有重复加", body.count("- 旧条目甲") == 1, body.count("- 旧条目甲"))

    print("② 前像与账本")
    imgs = os.listdir(os.path.join(vault, ".前像")) if os.path.isdir(os.path.join(vault, ".前像")) else []
    check("前像目录里有东西", bool(imgs), imgs)
    led = os.path.join(vault, ".写入账本")
    lines = []
    for name in os.listdir(led):
        lines += [json.loads(x) for x in io.open(os.path.join(led, name), encoding="utf-8") if x.strip()]
    check("账本记了写入者与模式", lines and lines[-1]["writer"] == "task:日报"
          and lines[-1]["mode"] == "merge", lines[-1] if lines else None)

    print("③ replace 缩短 → 拒绝（除非显式 --allow-shrink）")
    rc, d = run(vault, "记忆/热缓存.md", "--mode", "replace", "--text", "短\n")
    check("被拒（退出码 3，未改文件）", rc == 3 and d.get("refused") == "shrink", d)
    check("文件没被动", "- 新条目丙" in io.open(f, encoding="utf-8").read())
    rc, d = run(vault, "记忆/热缓存.md", "--mode", "replace", "--text", "短\n", "--allow-shrink")
    check("显式允许后就写了", rc == 0 and io.open(f, encoding="utf-8").read() == "短\n", d)

    print("④ append：纯追加")
    rc, d = run(vault, "记忆/热缓存.md", "--mode", "append", "--text", "又一行\n")
    check("追加成功", rc == 0 and io.open(f, encoding="utf-8").read() == "短\n又一行\n", d)

    print("⑤ 路径护栏 + dry-run")
    rc, d = run(vault, r"C:\Windows\notepad.exe", "--mode", "append", "--text", "x\n")
    check("vault 外的路径被挡", rc == 2 and not d.get("ok"), d)
    before = io.open(f, encoding="utf-8").read()
    rc, d = run(vault, "记忆/热缓存.md", "--mode", "append", "--text", "dry\n", "--dry-run")
    check("dry-run 不写盘", rc == 0 and d.get("dry_run") and io.open(f, encoding="utf-8").read() == before, d)
finally:
    shutil.rmtree(vault, ignore_errors=True)
    print("⑥ 临时目录已清理")

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
