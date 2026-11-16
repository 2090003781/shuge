"""`tools/convert_refs.py` 的验收：[[旧引用]] → @引用 的各种边界。

覆盖：正常转换 / 别名同名 / 别名不同（不转）/ 对不上（不转）/ 紧贴中文（补空格）/
代码围栏与行内代码（不动）/ 边真的建起来了 / 再跑一次不重复转（幂等）。
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
OK = True
TGT = "zz-转换-目标"
REF = "zz-转换-引用方"

BODY = u"""# 引用方

正常引用：见[[%s]]。
别名同名：见[[%s|%s]]。
别名不同：见[[%s|别的东西]]。
对不上：见[[zz-转换-不存在]]。
紧贴中文：在[[%s]]里面。

```
[[%s]]
```

行内代码：`[[%s]]`。

星号包裹：**[[%s]]**
带小节：见[[%s#P2]]。
""" % (TGT, TGT, TGT, TGT, TGT, TGT, TGT, TGT, TGT)


def call(path, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra)) if extra else ""))


def tool(*args):
    p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "convert_refs.py")] + list(args),
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    return p.returncode, p.stdout, p.stderr


files_before = call("/api/health")[1]["stats"]["files"]
made = []
try:
    print("① 造一篇被引用方 + 一篇各种边界的引用方")
    tgt = call("/api/files", "POST", {"name": TGT + ".md", "content": "# 目标\n\n随便写点。\n"})[1]["file_id"]
    ref = call("/api/files", "POST", {"name": REF + ".md", "content": BODY})[1]["file_id"]
    made += [tgt, ref]
    check("两篇都建好了", bool(tgt) and bool(ref))

    print("② 干跑（--only 引用方）")
    rc, out, err = tool("--only", ref, "--json")
    check("干跑退出码 0", rc == 0, err[-200:])
    d = json.loads(out)
    check("转换 5 处（含带小节那条）", d["converted"] == 5, d["converted"])
    check("其中 1 处带小节", d.get("with_section") == 1, d.get("with_section"))
    # 见/在 都是"词字符"，紧贴在 @ 前会让解析不到 → 补空格；紧贴中文后面同理
    check("补了 5 处空格（见/在 前后）", d["spaced_head"] + d["spaced_tail"] == 5,
          "head=%s tail=%s" % (d["spaced_head"], d["spaced_tail"]))
    check("别名不同的不转", d["skip_alias"] == 1, d["skip_alias"])
    check("对不上的不转", d["skip_miss"] == 1, d["skip_miss"])
    check("干跑没写库", call("/api/files/" + ref)[1]["file"]["content"] == BODY)

    print("③ 真转")
    rc, out, err = tool("--only", ref, "--apply")
    check("apply 退出码 0", rc == 0, err[-300:])
    text = call("/api/files/" + ref)[1]["file"]["content"]
    check("正常引用转了", ("见 @%s。" % TGT) in text, text[:60].replace("\n", "⏎"))
    check("别名同名也转了", text.count("见 @%s。" % TGT) == 2, text.count("@%s" % TGT))
    check("别名不同的留原样", "[[%s|别的东西]]" % TGT in text)
    check("对不上的留原样", "[[zz-转换-不存在]]" in text)
    check("紧贴中文补了空格", ("在 @%s 里面。" % TGT) in text,
          [l for l in text.splitlines() if "里面" in l])
    fence = text.split("```")[1]
    check("代码围栏里的没动", "[[%s]]" % TGT in fence, fence.strip()[:40])
    check("行内代码里的没动", "`[[%s]]`" % TGT in text)
    check("星号包裹的转了", ("**@%s**" % TGT) in text)
    check("带小节的转成 @名字#小节", ("见 @%s#P2。" % TGT) in text,
          [l for l in text.splitlines() if "#P2" in l])

    print("④ 引用边真的建起来了")
    f = call("/api/files/" + ref)[1]["file"]
    men = {m["id"]: m["anchor"] for m in (f.get("mentions") or [])}
    check("边指向目标篇", tgt in men, men)
    check("锚点是字面文本（本篇有带小节与不带小节两条）",
          set(a for i, a in men.items() if i == tgt) <= {TGT, TGT + "#P2"} and tgt in men,
          [a for i, a in men.items() if i == tgt])
    tb = call("/api/files/" + tgt)[1]["file"]["backlinks"]
    check("目标篇有反向链接", any(b["id"] == ref for b in (tb or [])), tb)

    print("⑤ 幂等：再干跑一次")
    rc, out5, err = tool("--only", ref, "--json")
    d2 = json.loads(out5)
    check("没有可转的了", d2["converted"] == 0, d2["converted"])

    print("⑥ 回滚：--restore 把这批还原回 [[ ]]")
    m = re.search(r"--restore (\S+)", out)
    check("apply 打印了回滚命令", bool(m), out[-160:])
    if m and os.path.isfile(m.group(1)):
        rc, out6, err6 = tool("--restore", m.group(1))
        check("restore 退出码 0", rc == 0, err6[-200:])
        back = call("/api/files/" + ref)[1]["file"]["content"]
        check("正文逐字还原", back == BODY, back[:50].replace("\n", "⏎"))
        f2 = call("/api/files/" + ref)[1]["file"]
        check("引用边也撤了", not (f2.get("mentions") or []), f2.get("mentions"))
finally:
    print("⑦ 清场")
    for fid in made:
        call("/api/files/" + fid, "DELETE")
    import sqlite3
    c = sqlite3.connect(os.path.join(ROOT, "data", "index.db"))
    c.execute("DELETE FROM tombstones WHERE display LIKE 'zz-%'")
    c.commit()
    c.close()
    after = call("/api/health")[1]["stats"]["files"]
    check("文件数复原", after == files_before, "%s → %s" % (files_before, after))

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
