#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""把一段本地脚本分块塞进客人（每块一条可在 Windows 侧粘贴执行的命令）。

为什么分块：客人里 sshd 时好时坏，`vm_put` 对小文件常被 reset；
而经 vm_exec 传 base64 是稳的（实测 'hello 枢阁' 一次成功），只是别把 3.4 KB 塞成一条。

用法：
  python scripts\vm_chunks.py <本地文件> <客人目标路径> [块大小]
输出：
  data\tmp\vmchunks.txt  —— 每条一行，依次粘进 vm_exec（第一条会先清空目标文件）
"""
import base64
import io
import os
import sys

OUT = r"D:\core\data\tmp\vmchunks.txt"


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    src, remote = sys.argv[1], sys.argv[2]
    size = int(sys.argv[3]) if len(sys.argv) > 3 else 800

    txt = io.open(src, encoding="utf-8").read()
    b64 = base64.b64encode(txt.encode("utf-8")).decode("ascii")
    chunks = [b64[i:i + size] for i in range(0, len(b64), size)]

    lines = []
    for i, c in enumerate(chunks):
        if i == 0:
            ps = ("[IO.File]::WriteAllText('%s', [Text.Encoding]::UTF8.GetString("
                  "[Convert]::FromBase64String('%s'))); 'ok 1/%d'" % (remote, c, len(chunks)))
        else:
            ps = ("$p='%s'; $b=[IO.File]::ReadAllText($p); [IO.File]::WriteAllText($p, $b + "
                  "[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s'))); "
                  "'ok %d/%d'" % (remote, c, i + 1, len(chunks)))
        lines.append('powershell -NoProfile -Command "%s"' % ps)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print("base64 %d 字符 → %d 块（每块 %d）；写法：先第一块，再依次后续块" % (len(b64), len(chunks), size))
    print("块清单写到：%s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
