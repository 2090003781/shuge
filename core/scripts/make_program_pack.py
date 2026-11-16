#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""把「内核 + 笔记」两个程序的**程序主体**打包成一个 zip 放桌面（不含数据）。

2026-10-06 辞玖：「把内核-笔记所有程序打包压缩放桌面，不包括数据、文件」。

为什么写成脚本而不是一次性命令：
  · 第一次手打 robocopy 时 `/XF core-host.exe` 把 **bin\core-host.exe 也排掉了**（那是必需的宿主程序！
    /XF 是按文件名匹配**任何层级**，我只想排根目录那个遗留件）—— 这种"少了一个必需文件"的包
    靠肉眼看不出，所以这里**用断言把关**：必需的必须在、禁止的必须不在、中文名必须能原样解回来。
  · 每次改完代码都可能要重新打包，脚本才可重跑。

排除规则（辞玖的要求 + 常规卫生）：
  · 目录：任何层级的 `data`、`.git`、`__pycache__`、`.pytest_cache`、`.mypy_cache`
  · 文件：`*.pyc`、`*.bak-*`、`*.bak-agentctl`，以及**平台根目录**那个遗留 `core-host.exe`（只排根目录）
包含：全部内核插件（辞玖选 A：全量带上）；不带 `.git`（辞玖选：不带）。

用法：
    python scripts\make_program_pack.py                 # 打到桌面
    python scripts\make_program_pack.py --out D:\x.zip  # 换落点
    python scripts\make_program_pack.py --keep-stage    # 留着暂存目录（排查用）
"""
import argparse
import io
import json
import os
import shutil
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(HERE)
NOTES = r"D:\core-notes"
README = os.path.join(HERE, "pack-readme.md")
PKG_NAME = "枢阁-程序包"

SKIP_DIRS = {"data", ".git", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules", ".vs"}
# ⚠️ **不能**把 "bin" 放进 SKIP_DIRS：平台自己的 `core\bin` 就是启动器/宿主所在（2026-10-09 一刀切下去，
#    平台从 182MB 变成 1.6MB —— bin 被整个排掉了）。
#    .NET/前端的构建产物只在 desktop 这棵树下排：那里才有 bin/obj/publish。
SKIP_DIRS_UNDER = {"desktop": {"bin", "obj", "publish"}}
SKIP_FILE_SUFFIX = (".pyc",)
SKIP_FILE_PAT = (".bak-",)
# 只排**根目录**这个遗留件（现役的是 bin\core-host.exe）
# 只排**根目录**的遗留件（/XF 那种按文件名匹配任意层级的写法曾经把 bin\core-host.exe 也排掉过，见文件头）。
# 2026-10-06 补：`legacy-core-host.exe.bak`（9-20 改名时留的 10 MB 遗留）以前被当普通文件打进包了 ——
#   包只该装"程序"，不该装历史备份。这里连 *.bak / *.bak-* / *.old 一起排掉（任何层级）。
ROOT_ONLY_SKIP = {"core-host.exe", "legacy-core-host.exe.bak"}
SKIP_FILE_PAT = SKIP_FILE_PAT + (".bak", ".bak-", ".old", ".orig")

# **手工开发/运维工具不进包**（辞玖 2026-10-06 18:08：「拿出来」）：
# 这些只在这台机器上、由人手动跑（改 DSH 插件源码、模拟老路、回填转录、跑转录自测），
# 不是"拿到程序包的人要用的东西"，而且里面写死了本机落点。判据：只有它们自己引用自己。
# 辞玖 2026-10-07：「压缩包里启动器不显眼 / 只用放 host exe」→
#   ① 只有宿主是"启动器"，内核由宿主按需拉起；② 内核 exe 不给（省包、也少一个能误双击的东西）；
#   ③ ledgercheck.exe 只被注释提到，没有任何运行时代码调它（同名能力走 /api/ledger/verify），算多余件。
NOT_IN_PACK_BIN = {"core.exe", "ledgercheck.exe"}
NOT_IN_PACK_ROOT = {"start_core.py"}   # 它只是"从 Job 里脱离"的包装，拿包的人用不到
DEV_ONLY = {
    "scripts/sim_dsh_transcript.mjs",
    "scripts/patch_dsh_transcript.mjs",
    "scripts/patch_dsh_transcript_retire.mjs",
    "scripts/smoke_dsh_transcript_plugin.mjs",
    "scripts/backfill_transcripts.py",
    "scripts/smoke_transcript.py",
    "scripts/smoke_hook_transcript.py",
}

# 打包后必须存在的（少一个就是废包）
MUST_HAVE = [
    # core/bin/core.exe 不再随包（辞玖 2026-10-07：只用放 host exe）
    "core/bin/core-host.exe",
    "启动枢阁.exe",                      # 根目录那个显眼的启动器（真 exe，按自身位置找宿主）
    "启动枢阁（备用）.cmd",                 # 备用启动器（万一 exe 被拦）
    "core/plugins/agent_plugin.py",
    "core/plugins/agentctl_plugin.py",
    "core/web/index.html",
    "core/config.yaml",
    "core/config.example.yaml",
    "core/internal/api/pluginmgr.go",
    # 主题这条线的三个"会在界面上出事"的件（2026-10-06：主题统一 / 实时 SSE / 按钮样式）
    "core/cmd/core-host/themehub.go",        # 实时广播（SSE）
    "core/scripts/theme_boot.js",            # 三个界面共用的主题引导（真正的源）
    "core/scripts/theme_light.py",           # 浅白·护眼主题的生成器
    "core/scripts/check_theme_unified.py",   # 主题自检（含"只有用户点击才往外推"的静态防线）
    "core/scripts/check_theme_browser.py",   # 真浏览器自检（实时 / 不振荡 / 写入次数）
    "core/scripts/check_agent_paths.py",     # agent 接入不许写死路径（占位符 → 本机识别结果）
    "core/scripts/check_launcher.py",       # 启动器：解到别处双击有没有反应（两种情形都验）
    "core/templates/agent/handoff.tpl",      # 接入手工单模板（路径是占位符）
    "core/scripts/agentctl.py",
    "core/templates/agent/handoff.tpl",
    "core-notes/src/server.py",
    "core-notes/web/index.html",
    "读我-先看这个.md",
    "core/scripts/pack-readme.md",        # 说明书的源（改文案改它）
    "core/templates/agent/examples/coze-handoff.md",  # 手工单样例（辞玖 2026-10-06：要进包）
]
# 一个都不许有的（数据/密钥/历史）
FORBIDDEN_IN_NAME = ("/data/", "data/", ".git/", "__pycache__", "token.json",
                     "permissions.json", "index.db", ".bak-agentctl", ".bak-")


def copy_tree(src, dst, root_only_skips=(), dev_only=(), not_in_root=(), not_in_bin=()):
    """拷一棵树，按规则排除；返回 (文件数, 字节数, 跳过的名字采样)"""
    n = b = 0
    skipped = []
    for dp, dns, fns in os.walk(src):
        rel_top = os.path.relpath(dp, src).replace("\\", "/").split("/")[0]
        extra = SKIP_DIRS_UNDER.get(rel_top, set())
        dns[:] = [d for d in dns if d not in SKIP_DIRS and d not in extra]
        rel_dir = os.path.relpath(dp, src)
        out_dir = dst if rel_dir == "." else os.path.join(dst, rel_dir)
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        for f in fns:
            if f.endswith(SKIP_FILE_SUFFIX) or any(p in f for p in SKIP_FILE_PAT):
                skipped.append(f)
                continue
            if rel_dir == "." and f in root_only_skips:
                skipped.append(f + "（仅根目录的遗留件）")
                continue
            if rel_dir == "." and f in not_in_root:
                skipped.append(f + "（启动器只留宿主，见说明）")
                continue
            if os.path.basename(rel_dir) == "bin" and f in not_in_bin:
                skipped.append(f + "（不是启动器 / 没人调，2026-10-07）")
                continue
            _rel = (os.path.join(rel_dir, f).replace("\\", "/")).lstrip("./")
            if _rel in dev_only:
                skipped.append(_rel + "（手工开发/运维工具）")
                continue
            sp, op = os.path.join(dp, f), os.path.join(out_dir, f)
            shutil.copy2(sp, op)
            n += 1
            b += os.path.getsize(op)
    return n, b, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="zip 落点（默认桌面）")
    ap.add_argument("--keep-stage", action="store_true", help="保留暂存目录")
    a = ap.parse_args()

    ts = time.strftime("%Y%m%d-%H%M")
    out = a.out or os.path.join(os.path.expanduser("~"), "Desktop", "%s-%s.zip" % (PKG_NAME, ts))
    stage = os.path.join(os.environ.get("TEMP", CORE), "sbpkg-" + ts)
    root = os.path.join(stage, PKG_NAME)
    if os.path.isdir(stage):
        shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(root)

    print("① 拷程序（排除：%s / %s / 根目录遗留 core-host.exe）"
          % ("、".join(sorted(SKIP_DIRS)), "*.pyc"))
    n1, b1, sk1 = copy_tree(CORE, os.path.join(root, "core"), ROOT_ONLY_SKIP, DEV_ONLY,
                                      NOT_IN_PACK_ROOT, NOT_IN_PACK_BIN)
    print("   平台：%d 个文件，%.1f MB（跳过 %d 个：%s）"
          % (n1, b1 / 1048576, len(sk1), "、".join(sk1[:6]) or "无"))
    if not os.path.isdir(NOTES):
        print("   ⚠️ 找不到笔记程序 %s —— 只打平台" % NOTES)
        n2 = b2 = 0
    else:
        n2, b2, sk2 = copy_tree(NOTES, os.path.join(root, "core-notes"))
        print("   笔记：%d 个文件，%.1f MB（跳过 %d 个：%s）"
              % (n2, b2 / 1048576, len(sk2), "、".join(sk2[:6]) or "无"))

    # ①-2 配置：把 core/config.yaml 里的 `vault.path` 改成**包内相对**（"core-notes"）。
    #
    # 为什么必须改（2026-10-11 虚拟机实测）：打包机上那份 config.yaml 写的是**这台机器**的绝对路径
    # （本机是 D:/个人数据/辞玖），包拷到别的机器/别的目录解压后，设置页「库目录」那一行就显示成
    # 一个**根本不存在的路径** —— 用户看到的就是"这个值没跟着走"。
    # 写成相对路径后，主进程按"应用根"解析（见 cmd/core-host/aboutinfo.go 的 vaultPathFromConfig），
    # 解压到哪都对。
    _cfg = os.path.join(root, "core", "config.yaml")
    if os.path.isfile(_cfg):
        _txt = io.open(_cfg, encoding="utf-8").read()
        _lines = _txt.split("\n")
        _out, _done = [], False
        for _i, _ln in enumerate(_lines):
            if (not _done) and _ln.strip() == "vault:":
                _out.append(_ln)
                # 下一行（缩进更深的 path:）换成包内相对
                if _i + 1 < len(_lines) and _lines[_i + 1].strip().startswith("path:"):
                    _indent = _lines[_i + 1][:len(_lines[_i + 1]) - len(_lines[_i + 1].lstrip())]
                    _out.append('%spath: "core-notes"   # 跟着包走：相对"应用根"解析（打包时改写）'
                                % _indent)
                    _lines[_i + 1] = None            # 标记已处理
                    _done = True
                    continue
            if _ln is None:
                continue
            _out.append(_ln)
        if _done:
            io.open(_cfg, "w", encoding="utf-8", newline="\n").write("\n".join(_out))
            print("   配置：core/config.yaml 的 vault.path → \"core-notes\"（包内相对）")
        else:
            print("   ⚠️ 没在 core/config.yaml 里找到 vault: 段 —— 那份配置可能带着打包机的绝对路径")

    # 说明书是**生成物**：把 `{{PACK_TS}}`（打包时间）与 `{{PLATFORM}}`（平台目录）填上，
    # 这样包里那份就是"这一次、这台机器"的真值（2026-10-06 辞玖：写一份详细的 readme）。
    _readme = io.open(README, encoding="utf-8").read()
    _readme = (_readme.replace("{{PACK_TS}}", time.strftime("%Y-%m-%d %H:%M"))
                      .replace("{{PLATFORM}}", CORE.replace("\\", "/")))
    io.open(os.path.join(root, "读我-先看这个.md"), "w", encoding="utf-8", newline="\n").write(_readme)

    # ①-5 启动器：**根目录**放一个显眼的快捷方式（辞玖 2026-10-07：「启动器没有显眼一点拿出来，
    # 我找不到启动器的位置，请把启动器放在 readme 那边，只用放 host exe」）。
    # 为什么用 .lnk 而不是把 exe 拷到根目录：exe 拷出去后按自身位置推平台根会推错（见 coreDirFromExe），
    # 快捷方式带 WorkingDirectory，双击才是"原位启动"。
    # 启动器：**真 exe**（`cmd/core-launch`）放到根目录。
    # 为什么不是 .lnk：快捷方式存的是**打包这台机器**的绝对路径，别人解压到别处双击就没反应
    # （辞玖 2026-10-07「我点这个启动咋没反应？」）；相对路径 .lnk 也试过，WScript 会按"当前目录"解析。
    # 真 exe 按**自己所在位置**找 core\bin\core-host.exe，解压到哪都能用。
    _launch = os.path.join(CORE, "bin", "launch.exe")
    if not os.path.isfile(_launch):
        raise RuntimeError("没有 bin\\launch.exe —— 先跑 scripts\\build_exe.ps1 -Target launch")
    shutil.copy2(_launch, os.path.join(root, "启动枢阁.exe"))

    # 桌面窗口版（辞玖 2026-10-09：「把 web 换成本地软件形式」）：
    # 自包含单文件（目标机器不用装 .NET），所以直接摆包根；同时放一份进 core\bin 便于原地启动。
    _shell = os.path.join(CORE, "desktop", "Shuge.Desktop", "bin", "Release",
                          "net10.0-windows", "win-x64", "publish", "shuge-app.exe")
    if os.path.isfile(_shell):
        shutil.copy2(_shell, os.path.join(root, "启动枢阁-窗口版.exe"))
        shutil.copy2(_shell, os.path.join(root, "core", "bin", "shuge-app.exe"))
        print("   桌面窗口版：启动枢阁-窗口版.exe（%.1f MB，自包含单文件）" % (os.path.getsize(_shell) / 1048576))
    else:
        print("   ⚠️ 没找到桌面壳产物 —— 先 dotnet publish（本次包里没有窗口版）：%s" % _shell)
    # 备用：极少数环境里 exe 被拦，留一个纯文本的 .cmd。
    # ⚠️ 必须写**相对自身**的路径（%~dp0）：写绝对路径的话，包换个地方解压就失效 —— 这正是
    #    .lnk 快捷方式翻车的原因（辞玖 2026-10-07「我点这个启动咋没反应？」）。
    # 路径按【本文件所在目录】算；两处都认（包根 / 平台根），所以放哪都能用。
    with io.open(os.path.join(root, "启动枢阁（备用）.cmd"), "w", encoding="gbk", newline="\r\n") as f:
        f.write("@echo off\r\n")
        f.write("rem 备用启动器：万一 exe 被拦，双击这个也能起。路径按【本文件所在目录】算，解压到哪都能用。\r\n")
        f.write("set HOST=%~dp0core\\bin\\core-host.exe\r\n")
        f.write("if not exist \"%HOST%\" set HOST=%~dp0bin\\core-host.exe\r\n")
        f.write('start "" "%HOST%" -open-web\r\n')
    print("   启动器：启动枢阁.exe（+ 备用 .cmd）→ core\\bin\\core-host.exe")

    print("② 压 zip（用 Python zipfile：中文名自动带 UTF-8 标志位）")
    if os.path.isfile(out):
        os.remove(out)
    # ⚠️ 别用 bsdtar（`tar -a -c -f`）：它写中文条目名**不带 UTF-8 标志位**（按本地 ANSI 写），
    #    Python/别的语言读回来是乱码，老解压工具也会花。zipfile 会给非 ASCII 名自动置 0x800。
    nz = 0
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for dp, dns, fns in os.walk(root):
                dns.sort()
                for f in sorted(fns):
                    p = os.path.join(dp, f)
                    z.write(p, os.path.relpath(p, stage))
                    nz += 1
    except Exception as e:
        print("   ❌ 压缩失败：%s: %s" % (type(e).__name__, e))
        return 1
    if not os.path.isfile(out) or nz == 0:
        print("   ❌ 没产出 zip（写入 %d 个）" % nz)
        return 1
    print("   %s（%.1f MB，写入 %d 个文件）" % (out, os.path.getsize(out) / 1048576, nz))

    # ③-0 **内容**防线（2026-10-06 辞玖「再检查一下，程序、插件等等，没有任何数据遗留」时补）：
    #   光看文件名不够 —— 抓下来的"搜索命中快照"（含真 file_id / 真文件名 / 真正文片段）就混过过一次。
    #   这里逐个打开包里的 .json/.jsonl，命中"库内数据形状"就拦下。
    data_hits = []
    parse_bad = []
    with zipfile.ZipFile(out) as zf:
        for e in zf.namelist():
            if e.endswith("/") or not e.lower().endswith((".json", ".jsonl")):
                continue
            try:
                t = zf.read(e).decode("utf-8", "replace")
            except Exception:
                continue
            # 判据要"结构级"（先解析再判），别用字符串撞 —— 接口文档里也有 "file_id"/"text" 这类**字段名**，
            # 第一版就是这么误报的：docs/openapi.json / docs/ops.json 被当成命中快照。
            try:
                obj = json.loads(t.lstrip("\ufeff"))     # 有些工具写出来的 JSON 带 BOM，先剥掉
            except Exception as ex:
                # ⚠️ 不许静默：这里以前写成"解析不了就 continue"，结果脚本没 import json 时
                #    防线**自己坏了却一直报通过**（2026-10-06 当场踩到）。现在凡是 .json 解析不了都记一笔。
                parse_bad.append("%s（不是合法 JSON：%s）" % (e, type(ex).__name__))
                continue
            items = obj if isinstance(obj, list) else (
                obj.get("hits") if isinstance(obj, dict) and isinstance(obj.get("hits"), list) else [obj])
            for it in items:
                if not isinstance(it, dict):
                    continue
                keys = set(it.keys())
                if {"file_id", "chunk_id"} & keys and isinstance(it.get("text", it.get("heading")), str):
                    data_hits.append("%s（像一条库内命中/块记录：%s）"
                                     % (e, ",".join(sorted(keys & {"file_id", "chunk_id", "text", "heading"}))))
                    break
                if len(str(it.get("text", ""))) > 300:
                    data_hits.append("%s（含 >300 字的疑似正文片段）" % e)
                    break
    if data_hits:
        print("   ❌ 包里有【库内数据形状】的文件：")
        for x in data_hits:
            print("      " + x)
    print("③ 验收（缺一个必需件 / 混进一个数据件，都算失败）")
    fails = list(data_hits) + list(parse_bad)
    with zipfile.ZipFile(out) as z:
        names = [x.filename for x in z.infolist()]
        nl = [x.replace("\\", "/") for x in names]
        pref = PKG_NAME + "/"
        rel = [x[len(pref):] for x in nl if x.startswith(pref)]
        print("   条目 %d 个（文件 %d，目录 %d）"
              % (len(names), sum(1 for x in z.infolist() if not x.is_dir()),
                 sum(1 for x in z.infolist() if x.is_dir())))
        for m in MUST_HAVE:
            if m not in rel:
                fails.append("缺少必需件：%s" % m)
        bad = [x for x in rel if any(p in x for p in FORBIDDEN_IN_NAME)]
        if bad:
            fails.append("混进了不该有的：%s" % bad[:5])
        # 中文名往返：把说明文件解回来，字节级比对
        try:
            got = z.read(pref + "读我-先看这个.md")
            want = _readme.encode("utf-8")   # 比对**写进去那份**（说明书含打包时间，不能拿源文件比）
            if got != want:
                fails.append("中文名/内容往返不一致（读我-先看这个.md）")
        except Exception as e:
            fails.append("中文名解不开：%s: %s" % (type(e).__name__, e))
        # 桌面窗口版：有就得包根 + bin 各一份（只放一半算失败）
        _root_shell = [x for x in rel if x.endswith("启动枢阁-窗口版.exe")]
        _bin_shell = [x for x in rel if x.endswith("core/bin/shuge-app.exe")]
        if _root_shell or _bin_shell:
            print("   桌面窗口版：包根 %s ｜ core/bin %s" % (bool(_root_shell), bool(_bin_shell)))
            if not (_root_shell and _bin_shell):
                fails.append("桌面窗口版只放了一半（包根与 core/bin 都必须有）")
        else:
            print("   （本次包里没有桌面窗口版）")

        # bin 下**启动器**的大小（防"少了宿主"这类；内核不随包，2026-10-07 起）
        for f in ("core/bin/core-host.exe",):
            try:
                info = [x for x in z.infolist() if x.filename.endswith(f.replace("/", "/"))
                        or x.filename.replace("\\", "/").endswith(f)]
                size = info[0].file_size if info else 0
                print("   %-46s %6.1f MB" % (f, size / 1048576))
                if size < 1 << 20:
                    fails.append("%s 只有 %d 字节（像是被排除规则误伤）" % (f, size))
            except Exception as e:
                fails.append("读 %s 失败：%s" % (f, e))

    if not a.keep_stage:
        shutil.rmtree(stage, ignore_errors=True)
    else:
        print("   暂存留着：%s" % stage)
    if fails:
        print("\n❌ 验收不过：\n   - " + "\n   - ".join(fails))
        return 1
    print("\n✅ 全部通过：必需件在、数据/密钥/历史都不在、中文名能解回来")
    print("   桌面产物：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
