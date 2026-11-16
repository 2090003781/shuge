#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""把一段 PowerShell 脚本用 base64 塞进客人，然后跑它（绕开 scp 小文件被重置的问题）。

为什么要有这个：
  · 这台虚拟机的 SSH 通道**大文件能传、小文件反而常被 reset**（实测：93 MB 的包传成功，
    3 KB 的 .ps1 连传四次都 "Connection reset by peer"）；
  · 经 vm_exec 传脚本又会撞上"引号套引号"的地狱。
所以：主机侧把脚本 base64（纯 ASCII、不含引号），在客人里 `[Text.Encoding]::UTF8.GetString` 还原。

用法：
  python scripts\vm_push_script.py <本地 .ps1> [客人路径] [超时秒]
  python scripts\vm_push_script.py --show <本地 .ps1>        # 只打印 base64（手工贴用）
"""
import base64
import os
import subprocess
import sys

WSL = ["wsl", "-d", "Ubuntu", "--", "bash", "-lc"]


def vm_exec(ps_cmd, timeout=120):
    """借 MCP 的 vm_exec 不方便（那是工具），直接用 harness 里那支 vm-ssh.py。"""
    safe = ps_cmd.replace("'", "'\\''")
    cmd = "python3 /mnt/e/VMs/_tools/vm-ssh.py run '%s'" % safe
    r = subprocess.run(WSL + [cmd], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    src = sys.argv[1]
    with open(src, encoding="utf-8") as f:
        txt = f.read()
    b64 = base64.b64encode(txt.encode("utf-8")).decode("ascii")

    if src == "--show" or (len(sys.argv) > 1 and sys.argv[1] == "--show"):
        print(b64)
        return 0

    remote = sys.argv[2] if len(sys.argv) > 2 else "C:\\vmtest.ps1"
    timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 120

    # 客人里：base64 → UTF8 文本 → 写文件（不经过任何引号嵌套）
    ps = ("$b='%s'; [IO.File]::WriteAllText('%s', [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b)))"
          % (b64, remote))
    rc, out, err = vm_exec(ps, timeout)
    print("rc=%s out=%s err=%s" % (rc, out[:200], err[:200]))
    return rc


if __name__ == "__main__":
    sys.exit(main())
