#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**换台机器 / 换个目录解压，那一页的数字会不会跟着变**（可重跑）。

辞玖 2026-10-11：「这部分在新机上不同位置能自动更新吗」。
光看代码不算数，这里**真的把程序包解到另一个目录再跑起来**，然后问它自己：
  ① `/api/host/about` 里每一行是不是都指向**新目录**（应用根 / 平台目录 / 库目录 / 用户目录）；
  ② 有没有哪一行还写着老路径（**这就是"没自动更新"**）；
  ③ 界面那一页（/settings/）能不能打开。

做法要点（都是踩出来的）：
  · 用 `COREHOST_LOCK_NAME` 换单实例锁名 —— 否则跟本机现役实例撞锁，一启动就退出；
  · 用 `-port` 指一个空端口 —— 不跟现役的 8790 打架；
  · 库里 `src/server.py` 找不到时它会尝试装依赖，别让它真去装：只看 host 自报的路径对不对。

用法：python scripts\check_portable_paths.py [--keep]
"""
import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

CORE = r"D:\core"                     # 现役平台（要拿来当"模板"）
SRC_PACK = os.path.join(CORE, "data", "tmp", "portable-src")   # 从这棵树拷（含 bin\core-host.exe）
PORT = 18899
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
fails = []


def chk(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else "  <- " + str(extra)[:220]))
    if not cond:
        fails.append(name)


def get(url, timeout=12):
    return json.loads(urllib.request.urlopen(url, timeout=timeout).read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="跑完别删临时目录（排障用）")
    a = ap.parse_args()

    root = os.path.join(os.environ.get("TEMP", r"C:\Windows\Temp"), "shuge-portable-%d" % int(time.time()))
    app = os.path.join(root, "some", "other", "place", "枢阁")     # 故意放深一点、路径里带中文
    core_new = os.path.join(app, "core")
    notes_new = os.path.join(app, "core-notes")
    os.makedirs(core_new, exist_ok=True)

    print("① 先把最小的一套拷到**另一个目录**：%s" % app)
    # 主进程 exe + web + config.yaml：够它起来并把路径说清楚
    for rel in ("bin/core-host.exe", "config.yaml"):
        src = os.path.join(CORE, rel)
        dst = os.path.join(core_new, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    shutil.copytree(os.path.join(CORE, "web"), os.path.join(core_new, "web"), dirs_exist_ok=True)
    # 一个"空的库"：不真跑库，只看路径解析
    os.makedirs(os.path.join(notes_new, "src"), exist_ok=True)
    open(os.path.join(notes_new, "src", "server.py"), "w", encoding="utf-8").write("# 空壳，只为让路径解析认它\n")
    # ⚠️ 配置就用**包里那种写法**（相对"应用根"）—— 2026-10-12 虚拟机实测的教训：
    #    以前这个自检**自己重写** config.yaml（写成新根的绝对路径），所以没发现
    #    "包里那份配置带着打包机绝对路径"这个真问题。现在两条都验：
    #      ① 相对写法（新包）→ 应该跟着新根走；
    #      ② 老包的绝对写法（别人机器的路径）→ **实际生效的**仍应是新根的 core-notes，
    #         并且 about 要如实把 config 里那条报出来（vault_config）。
    with open(os.path.join(core_new, "config.yaml"), "w", encoding="utf-8") as f:
        f.write("resources:\n  vault:\n    path: \"core-notes\"   # 跟着包走\n")
    print("   拷好：%s" % core_new)

    print("② 在那边起主进程（换锁名 + 换端口，不碰现役实例）")
    env = dict(os.environ)
    env["COREHOST_LOCK_NAME"] = "core-host-portable-test"
    env["COREHOST_NOTES_DIR"] = notes_new          # 库也指到新根，别去动本机那套
    # ⚠️ 主进程**没有 -port 参数**，而且 listenControl 是**从候选表（8790…）里挑第一个空的**，
    #    data/host.port 是它挑完之后写下去的（不是读它来定端口）。
    #    所以这里**不预设端口**：起完之后读它自己写的 host.port（2026-10-11 连踩两次）。
    os.makedirs(os.path.join(core_new, "data"), exist_ok=True)
    for stale in ("host.port", "serve.port"):
        try:
            os.remove(os.path.join(core_new, "data", stale))
        except OSError:
            pass
    exe = os.path.join(core_new, "bin", "core-host.exe")
    # ⚠️ 必须带 -serve：不带的话主进程是"一次性"语义（拉起内核就退），进程立刻 exit 0，
    #    看着像"起不来"（2026-10-11 踩了半天）。
    proc = subprocess.Popen([exe, "-dir", core_new, "-serve"], cwd=core_new, env=env,
                            creationflags=NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base, up, port = "", False, 0
        log = os.path.join(core_new, "data", "logs", "host.log")
        for _ in range(60):
            time.sleep(0.5)
            try:
                port = int(open(os.path.join(core_new, "data", "host.port"), encoding="utf-8").read().strip())
            except Exception:
                port = 0
            if port:
                base = "http://127.0.0.1:%d" % port
                try:
                    get(base + "/healthz", timeout=3)
                    up = True
                    break
                except Exception:
                    pass
        chk("那边的主进程起来了（%s）" % base, up)
        if not up:
            if os.path.exists(log):
                print("   它的日志尾巴：")
                for ln in open(log, encoding="utf-8", errors="replace").read().splitlines()[-6:]:
                    print("     " + ln[:160])
            return 1
        chk("它听的是自己的端口（不是现役那个 8790）", port != 8790, port)

        ab = get(base + "/api/host/about")
        print("   它自己报的：")
        for k in ("app_root", "core_dir", "vault", "user_dir", "console"):
            print("     %-10s %s" % (k, ab.get(k)))

        print("③ 每一行是不是都指向**新目录**")
        norm = lambda p: (p or "").replace("/", "\\").rstrip("\\").lower()
        chk("应用根 = 新应用根", norm(ab.get("app_root")) == norm(app), ab.get("app_root"))
        chk("平台目录 = 新平台目录", norm(ab.get("core_dir")) == norm(core_new), ab.get("core_dir"))
        chk("库目录 = 新的库目录（config 里是相对写法时）",
            norm(ab.get("vault")) == norm(notes_new), ab.get("vault"))
        chk("库目录来自 config.yaml（相对路径按应用根解析）",
            ab.get("vault_from") == "config.yaml", ab.get("vault_from"))
        chk("config 里那条解析出来也是新根下的 core-notes",
            norm(ab.get("vault_config")) == norm(notes_new), ab.get("vault_config"))
        chk("界面地址用的是它自己的端口", (":%d" % port) in (ab.get("console") or ""), ab.get("console"))

        print("④ 有没有哪一行还写着老路径（这就是「没自动更新」）")
        old_core = norm(CORE)
        stale = {k: v for k, v in ab.items()
                 if isinstance(v, str) and old_core in norm(v)}
        chk("没有一行还指向 D:\\core", not stale, stale)
        chk("用户目录还是本机用户（不写死）", norm(ab.get("user_dir")) == norm(os.environ.get("USERPROFILE", "")),
            ab.get("user_dir"))

        print("④-2 老包写法（config 里写着**别的机器**的绝对路径）")
        with open(os.path.join(core_new, "config.yaml"), "w", encoding="utf-8") as f:
            f.write("resources:\n  vault:\n    path: \"D:/另一台机器的旧路径/不存在的库\"\n")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        time.sleep(1)
        proc = subprocess.Popen([exe, "-dir", core_new, "-serve"], cwd=core_new, env=env,
                                creationflags=NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        up2 = False
        for _ in range(60):
            time.sleep(0.5)
            try:
                ab2 = get(base + "/api/host/about")
                up2 = True
                break
            except Exception:
                pass
        chk("换回老配置后主进程还在答话", up2)
        if up2:
            chk("实际生效的库目录**仍然是**新根下的 core-notes",
                norm(ab2.get("vault")) == norm(notes_new), ab2.get("vault"))
            chk("config 里那条被如实报出来（不冒充库目录）",
                norm(ab2.get("vault_config")) != norm(ab2.get("vault")), ab2.get("vault_config"))
            chk("并标明那条路径在这台机器上不存在", ab2.get("vault_config_ok") is False, ab2.get("vault_config_ok"))

        print("⑤ 设置那一页能不能开")
        with urllib.request.urlopen(base + "/settings/", timeout=12) as r:
            html = r.read().decode("utf-8", "replace")
        chk("GET /settings/ 200 且有那一页的内容", r.status == 200 and "关于枢阁" in html, r.status)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        if not a.keep:
            shutil.rmtree(root, ignore_errors=True)
        else:
            print("（临时目录留着：%s）" % root)

    print("")
    if fails:
        print("❌ %d 项不过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("✅ 换个目录解压：应用根 / 平台目录 / 库目录 / 界面地址都跟着变，没有一行写死")
    return 0


if __name__ == "__main__":
    sys.exit(main())
