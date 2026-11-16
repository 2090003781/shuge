# -*- coding: utf-8 -*-
"""忘了库密码时的"破窗"工具（**命令行**，要有磁盘权限才能跑）。

为什么不在界面上给这个入口（辞玖 2026-09-28 的话："右键重设密码的话这个密码不等于随时被改吗"）：
GUI 里要是"上锁状态也能重设"，那这把锁就形同虚设 —— 谁点一下都能把密码换掉。
所以界面上的「重设库密码」**必须先解锁**（=你知道当前密码）；真忘了密码走这里。

它做的事：把库密码的密钥材料（kdf_algo / kdf_salt / verify_blob）清空 —— 之后到页面上重新设一个新密码。
已经加密的私密篇在换密码后解不开，用 维护菜单 →「🩹 修复私密篇」按新密钥从磁盘明文重新加密即可。

用法：
  python tools/lock_force_reset.py          # 只说明后果，不动
  python tools/lock_force_reset.py --yes    # 真的清
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from db import Database  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="确认要清掉密钥材料")
    a = ap.parse_args()
    d = Database()
    before = (d.manifest() or {}).get("kdf_algo") or "（本来就没设密码）"
    if not a.yes:
        print("这会清掉库密码的密钥材料。已加密的私密篇将无法解密")
        print("（之后可用 维护菜单 →「🩹 修复私密篇」从磁盘明文重新加密捞回来）。")
        print("当前：kdf_algo=%s" % before)
        print("确认要清请加 --yes")
        return 2
    d.set_manifest(kdf_algo="", kdf_salt="", verify_blob="")
    d.close()
    print("已清掉密钥材料（kdf_algo=%s → 空）。" % before)
    print("下一步：打开页面 → 右下角 🔒 设一个新密码；要恢复私密篇就跑一次「🩹 修复私密篇」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
