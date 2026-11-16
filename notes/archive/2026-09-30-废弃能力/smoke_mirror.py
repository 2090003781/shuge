"""库文件夹 ⇄ vault 双向同步自测（临时目录，不碰真环境）。

  python tools/smoke_mirror.py

十种情形：只在一侧、内容一样（同/不同 mtime）、两边都改了（各赢一次）、
忽略项、私密前缀跳过、删除会被复制回来（v1 的已知行为）、预演不写盘、报告计数自洽。
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, "src")
import mirror                     # noqa: E402

OK = True


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:150] if extra else ""))


def put(root, rel, text, mtime=None):
    p = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(text.encode("utf-8"))
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


def read(p):
    return open(p, "rb").read().decode("utf-8")


lib = tempfile.mkdtemp(prefix="mir-lib-")
vault = tempfile.mkdtemp(prefix="mir-vault-")
now = time.time()
try:
    print("① 只在一侧 → 复制过去")
    put(lib, "项目/甲.md", "库里的甲", now - 100)
    put(vault, "项目/乙.md", "vault 的乙", now - 100)
    r = mirror.sync(lib, vault)
    check("库独有的进了 vault", read(os.path.join(vault, "项目", "甲.md")) == "库里的甲", r["only_library"])
    check("vault 独有的进了库", read(os.path.join(lib, "项目", "乙.md")) == "vault 的乙", r["only_vault"])
    check("计数对得上", r["only_library"] == 1 and r["only_vault"] == 1, r)

    print("② 内容一样")
    put(lib, "项目/丙.md", "一样的内容", now - 50)
    put(vault, "项目/丙.md", "一样的内容", now - 50)
    r = mirror.sync(lib, vault)
    check("同内容同时间 → 不算改动", r["same"] >= 1 and not r["copied_to_vault"]
          and not r["copied_to_library"], r["same"])
    put(vault, "项目/丙.md", "一样的内容", now - 10)
    r = mirror.sync(lib, vault)
    check("同内容不同时间 → 只对齐 mtime，不复制", r["mtime_aligned"] == 1 and not r["copied_to_vault"],
          (r["mtime_aligned"], r["copied_to_vault"]))
    check("对齐后两边 mtime 一致",
          abs(os.path.getmtime(os.path.join(lib, "项目", "丙.md"))
              - os.path.getmtime(os.path.join(vault, "项目", "丙.md"))) < 1)

    print("③ 两边都改了 → 谁新谁赢（各来一次）")
    put(lib, "项目/丁.md", "库的新版本", now - 5)
    put(vault, "项目/丁.md", "vault 的旧版本", now - 500)
    mirror.sync(lib, vault)
    check("库新的赢（覆盖 vault）", read(os.path.join(vault, "项目", "丁.md")) == "库的新版本")
    put(lib, "项目/戊.md", "库的旧版本", now - 500)
    put(vault, "项目/戊.md", "vault 的新版本", now - 5)
    r = mirror.sync(lib, vault)
    check("vault 新的赢（覆盖库）", read(os.path.join(lib, "项目", "戊.md")) == "vault 的新版本",
          r["newer_vault"])

    print("④ 忽略项与私密前缀")
    put(vault, ".obsidian/workspace.json", "{}", now)
    put(vault, "tmp/工具.cjs", "x", now)
    put(vault, "个人/私密/密钥.md", "明文密钥", now)
    r = mirror.sync(lib, vault)
    check("点开头的不进库", not os.path.exists(os.path.join(lib, ".obsidian")))
    check("tmp 前缀不进库", not os.path.exists(os.path.join(lib, "tmp", "工具.cjs")))
    check("私密前缀整棵跳过（vault 那边也还在）",
          os.path.isfile(os.path.join(vault, "个人", "私密", "密钥.md"))
          and not os.path.exists(os.path.join(lib, "个人", "私密", "密钥.md")), r["private_skipped"])
    check("忽略/私密计数报出来", r["ignored"] >= 2 and r["private_skipped"] == 1,
          (r["ignored"], r["private_skipped"]))

    print("⑤ v1 的已知行为：一边删了会被复制回来")
    os.remove(os.path.join(vault, "项目", "甲.md"))
    r = mirror.sync(lib, vault)
    check("删除没传播（从库那边补回来了）",
          os.path.isfile(os.path.join(vault, "项目", "甲.md")), r["copied_to_vault"][:3])

    print("⑥ 预演不写盘")
    put(vault, "项目/己.md", "只该在预演里出现", now)
    r = mirror.sync(lib, vault, apply=False)
    check("预演报告说要复制", "项目/己.md" in r["copied_to_library"], r["copied_to_library"][:3])
    check("但盘上没写", not os.path.exists(os.path.join(lib, "项目", "己.md")))
    mirror.sync(lib, vault)
    check("真跑就写上了", os.path.isfile(os.path.join(lib, "项目", "己.md")))

    print("⑦ 真相保护三道闸")
    import tempfile as _tf
    bak = _tf.mkdtemp(prefix="mir-bak-")
    # 长 → 短：拒绝覆盖并记冲突
    big = "长文件\n" * 200
    put(lib, "项目/长.md", big, now - 10)
    put(vault, "项目/长.md", big, now - 10)
    mirror.sync(lib, vault)
    put(lib, "项目/长.md", "短", now)                 # 库那份变成 3 字节、且更新
    r = mirror.sync(lib, vault, backup_root=bak)
    check("短覆盖长被拒（vault 那份没被改小）",
          len(open(os.path.join(vault, "项目", "长.md"), "rb").read()) > 100
          and any(c["rel"] == "项目/长.md" for c in r["conflicts"]), r["conflicts"][-1:])

    # 正常覆盖 → 覆盖前先备份
    put(lib, "项目/短.md", "旧内容旧内容旧内容", now - 10)
    put(vault, "项目/短.md", "旧内容旧内容旧内容", now - 10)
    mirror.sync(lib, vault)
    put(lib, "项目/短.md", "新内容新内容新内容", now)
    r = mirror.sync(lib, vault, backup_root=bak)
    check("覆盖成功且留了备份", r["backed_up"] and
          any("项目" in p for p in r["backed_up"]), r["backed_up"][:2])
    check("备份里是覆盖前的内容",
          any(open(p, "rb").read().decode("utf-8") == "旧内容旧内容旧内容"
              for p in r["backed_up"] if "短.md" in p))

    # 只追加目录：两边不同也不动
    put(lib, "日志/reasonix-raw/会话.md", "库里那一份\n" * 50, now - 10)
    put(vault, "日志/reasonix-raw/会话.md", "只有开头", now)
    r = mirror.sync(lib, vault, backup_root=bak)
    check("只追加目录两边不同 → 谁也没动",
          os.path.getsize(os.path.join(vault, "日志", "reasonix-raw", "会话.md")) == len("只有开头".encode())
          and any(c["rel"].endswith("会话.md") for c in r["conflicts"]), r["conflicts"][-1:])

    # 收尾前把"故意留下的冲突"清掉（只追加那份：删掉 vault 侧，库那份会照常补齐回去）
    os.remove(os.path.join(vault, "日志", "reasonix-raw", "会话.md"))
    mirror.sync(lib, vault, backup_root=bak)

    print("⑧ 收尾：再跑一轮应当「无事可做」")
    r = mirror.sync(lib, vault)
    check("没有复制、没有冲突", not r["copied_to_vault"] and not r["copied_to_library"]
          and not r["conflicts"], (r["copied_to_vault"], r["copied_to_library"], r["conflicts"]))
    check("没有错误", not r["errors"], r["errors"][:2])
finally:
    shutil.rmtree(lib, ignore_errors=True)
    shutil.rmtree(vault, ignore_errors=True)
    shutil.rmtree(bak, ignore_errors=True)
    print("⑨ 临时目录已清理")

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
