#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""页面脚本语法自查：把 HTML（或 Go 里那段 HTML 原始字符串）中的每个内联 <script> 抠出来，
逐个交给 node --check。改完页面脚本跑一下，比在浏览器里看"整格 JS 死了"便宜得多。

用法：
  python scripts\check_page_js.py D:\core-notes\web\index.html
  python scripts\check_page_js.py --go D:\core\cmd\core-host\page.go hostPageHTML
"""
import io
import os
import re
import subprocess
import sys
import tempfile

SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def html_of(path, const_name=None):
    with io.open(path, "r", encoding="utf-8", newline="") as f:
        src = f.read()
    if not const_name:
        return src
    m = re.search(r"const\s+%s\s*=\s*`" % re.escape(const_name), src)
    if not m:
        print("找不到 %s 的原始字符串" % const_name)
        return ""
    i = m.end()
    j = src.index("`", i)          # 原始字符串里不会出现反引号
    return src[i:j]


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--go":
        path, const = args[1], args[2]
        html = html_of(path, const)
        title = "%s:%s" % (os.path.basename(path), const)
    else:
        path = args[0]
        html = html_of(path)
        title = os.path.basename(path)
    blocks = SCRIPT_RE.findall(html)
    if not blocks:
        print("%s：没有内联脚本" % title)
        return 0
    bad = 0
    tmp = tempfile.mkdtemp(prefix="shuge-js-")
    for i, b in enumerate(blocks):
        if not b.strip():
            continue
        f = os.path.join(tmp, "block%d.js" % i)
        with io.open(f, "w", encoding="utf-8") as fh:
            fh.write(b)
        r = subprocess.run(["node", "--check", f], capture_output=True, text=True,
                           shell=(os.name == "nt"))
        if r.returncode != 0:
            bad += 1
            print("x %s 第 %d 段脚本语法错：" % (title, i + 1))
            print("\n".join(("    " + x) for x in (r.stderr or "").splitlines()[:6]))
    print("%s：%d 段内联脚本，%s" % (title, len(blocks), "全过" if not bad else "%d 段有问题" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
