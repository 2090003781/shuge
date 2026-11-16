"""引用联动冒烟：改名时自动改写引用处，删除时把引用标成不可用。

自建自清；顺带验证"边界不误伤"（@abc 不会吃掉 @abcd 里的那截）。
"""
import json
import re
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
ok_all = True
SRC = "zz-引用方.md"
TGT1 = "zz-引用目标V2.md"       # 新名故意以旧名为前缀，考验边界
TGT0 = "zz-引用目标.md"
D0 = TGT0[:-3]                  # 提及里写的是短名（去 .md）
D1 = TGT1[:-3]
LONGER = "zz-引用目标V2尾巴"     # 一个"更长、没有边界"的名字，不该被改


def call(path, method="GET", body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:170] if extra else ""))


files_before = call("/api/health")[1]["stats"]["files"]
made = []

try:
    print("① 建被引用方 + 引用方")
    t0 = call("/api/files", "POST", {"name": TGT0, "content": "# 目标\n\n正文。\n"})[1]["file_id"]
    made.append(t0)
    body = ("# 引用方\n\n"
            "正常引用：@%s 后面有空格。\n"
            "带标点：@%s，逗号也算边界。\n"
            "更长的名字：@%s 不该被动。\n"
            "代码块里的不动：\n\n```\n@%s\n```\n" % (D0, D0, LONGER, D0))
    src = call("/api/files", "POST", {"name": SRC, "content": body})[1]["file_id"]
    made.append(src)
    f = call("/api/files/" + src)[1]["file"]
    check("引用建好了（1 条边）", len(f["mentions"]) == 1, f["mentions"])

    pend_before = call("/api/health")[1]["pending_vectors"]

    print("② 改名（旧名是新名的前缀，考验边界）")
    st, r = call("/api/files/%s/rename" % t0, "POST", {"name": TGT1})
    check("改名 200", st == 200, r)
    vv = (r.get("refs") or {}).get("vectors") or {}
    # 改名只改引用方正文，提及名不进嵌入文本 → 向量本就该继续用（记录 §25）
    check("改引用名不必重算向量（indexed=0）", vv.get("indexed", 0) == 0, vv)
    check("没有留下向量缺口",
          call("/api/health")[1]["pending_vectors"] <= pend_before,
          "%s vs %s" % (call("/api/health")[1]["pending_vectors"], pend_before))
    rw = (r.get("refs") or {}).get("rewritten") or {}
    check("报告了引用被改写", sum(rw.values()) == 2, rw)

    text = call("/api/files/" + src)[1]["file"]["content"]
    check("带空格的引用改成了新名", ("@%s 后面有空格。" % D1) in text, text[:120])
    check("带标点的引用也改了", ("@%s，逗号也算边界。" % D1) in text)
    # 正文里还剩的 @旧名 只该有两处：代码块里那处（不动）+ 更长名字的前缀（不动）
    # "以旧名独立出现（后面是边界）"的只剩代码块那一处
    # （@旧名V2 是前缀不算；@旧名V2尾巴 后面接的是 V 也不算）
    lone = re.findall(r"@%s(?![^\s@\[\]()（）,，。；;：:！!？?*`#>|/\\])" % re.escape(D0), text)
    check("独立出现的 @旧名 只剩代码块那处", len(lone) == 1, "%d 处" % len(lone))
    check("更长的那条名字没被误伤", ("@%s 不该被动" % LONGER) in text)
    check("代码块里的没被改", ("```\n@%s\n```" % D0) in text)
    f2 = call("/api/files/" + src)[1]["file"]
    check("边还在、指向新名", len(f2["mentions"]) == 1 and f2["mentions"][0]["name"] == TGT1,
          f2["mentions"])

    print("③ 删除被引用方 → 引用标成不可用")
    call("/api/files/" + t0, "DELETE")
    f3 = call("/api/files/" + src)[1]["file"]
    check("该边已随删除清掉", not f3["mentions"], f3["mentions"])
    check("正文里报出 deleted_refs", D1 in (f3.get("deleted_refs") or []),
          f3.get("deleted_refs"))
    check("正文本身没被改（删除不改文本）", ("@%s 后面有空格。" % D1) in f3["content"])

    print("④ 同名重建 → 墓碑要撤掉（别把活的当死的）")
    t2 = call("/api/files", "POST", {"name": TGT1, "content": "# 又回来了\n"})[1]["file_id"]
    made.append(t2)
    f4 = call("/api/files/" + src)[1]["file"]
    check("重建后不再是已删除", D1 not in (f4.get("deleted_refs") or []),
          f4.get("deleted_refs"))
finally:
    print("⑤ 清场")
    for fid in made:
        call("/api/files/" + fid, "DELETE")
    sys.path.insert(0, "src")
    from db import Database
    d = Database()
    n = d.conn.execute("DELETE FROM tombstones WHERE display LIKE 'zz-%'").rowcount
    d.conn.commit()
    d.close()
    h = call("/api/health")[1]["stats"]
    check("文件数复原", h["files"] == files_before, "%s vs %s" % (h["files"], files_before))
    print("   （顺手清了 %d 条测试墓碑）" % n)

print("\n========  结论：%s" % ("全部通过" if ok_all else "有失败项"))
sys.exit(0 if ok_all else 1)
