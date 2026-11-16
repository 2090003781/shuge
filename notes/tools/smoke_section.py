"""小节级引用的验收：`@目标#小节`。

覆盖：解析（名字+小节）/ 边上的锚点是字面文本 / 反向链接带小节 / 按小节取内容 /
改名时小节跟着保留 / 删除后被引用方标成不可用 / 嵌入掩码把名字+小节一起吃掉。
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from mentions import mask_mentions, parse_refs, split_anchor   # noqa: E402

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
OK = True
TGT = "zz-小节-目标"
REF = "zz-小节-引用方"


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


print("⓪ 解析与掩码（纯函数）")
refs = parse_refs("见 @原则区#P12 和 @错误库 ，还有 @原则区#P12")
check("带小节的引用解析出名字+小节",
      refs[0]["name"] == "原则区" and refs[0]["section"] == "P12" and refs[0]["anchor"] == "原则区#P12",
      refs)
check("同名不同小节算两条", len(refs) == 2, refs)
check("不带小节的照旧", refs[1]["anchor"] == "错误库", refs[1])
check("split_anchor", split_anchor("原则区#P12") == ("原则区", "P12"), split_anchor("原则区#P12"))
m = mask_mentions("见 @原则区#P12 那条。")
check("嵌入掩码把名字和小节一起吃掉", "原则区" not in m and "P12" not in m and "（引用）" in m, m)

TARGET = ("# 目标\n\n## P1 第一节\n\n这里是小节一的正文 ZZSECONE。\n\n"
          "## P2 第二节\n\n这里是小节二的正文 ZZSECTWO，唯一标记。\n\n"
          "## P3 第三节\n\n这里是小节三的正文 ZZSECTHREE。\n")

files_before = call("/api/health")[1]["stats"]["files"]
made = []
try:
    print("① 造目标篇（三节）+ 引用方（引 P2）")
    tgt = call("/api/files", "POST", {"name": TGT + ".md", "content": TARGET})[1]["file_id"]
    ref = call("/api/files", "POST",
               {"name": REF + ".md", "content": "# 引用方\n\n见 @%s#P2 那节。\n" % TGT})[1]["file_id"]
    made += [tgt, ref]

    f = call("/api/files/" + ref)[1]["file"]
    men = {m2["id"]: m2["anchor"] for m2 in (f.get("mentions") or [])}
    check("边指向目标篇", tgt in men, men)
    check("锚点是字面文本（带小节）", men.get(tgt) == "%s#P2" % TGT, men.get(tgt))

    tb = call("/api/files/" + tgt)[1]["file"].get("backlinks") or []
    check("反向链接带出小节", any(b["id"] == ref and b["anchor"] == "%s#P2" % TGT for b in tb), tb)

    print("② 按小节取内容（引用颗粒度到小节的取回方式）")
    a = call("/api/files/%s/article?section=P2" % tgt)[1]["article"]
    check("mode=section", a.get("mode") == "section", a.get("mode"))
    check("含第二节内容", "ZZSECTWO" in a.get("text", ""))
    check("不含第一节/第三节", "ZZSECONE" not in a.get("text", "")
          and "ZZSECTHREE" not in a.get("text", ""))
    check("小节标题对得上", str(a.get("heading", "")).startswith("P2"), a.get("heading"))
    bad = call("/api/files/%s/article?section=%s"
               % (tgt, urllib.parse.quote("不存在的小节")))[1]["article"]
    check("小节找不到时不会硬凑", bad.get("mode") in ("full", "section"), bad.get("mode"))

    print("②a 小节名宽容匹配：空格 / [标签] 前缀都不影响")
    t4 = call("/api/files", "POST", {"name": "zz-小节-宽容.md",
                                     "content": "# 宽容\n\n## AI 模型 API Key\n\n甲甲。\n\n"
                                                "## [视觉] 看图优先本地模型\n\n乙乙。\n"})[1]["file_id"]
    r4 = call("/api/files", "POST", {"name": "zz-小节-宽容方.md",
                                     "content": "# 引用方\n\n见 @zz-小节-宽容#AI模型 ，"
                                                "还有 @zz-小节-宽容#看图优先本地模型 。\n"})[1]["file_id"]
    made += [t4, r4]
    men4 = call("/api/files/" + r4)[1]["file"].get("mentions") or []
    check("空格差异也算同一节（AI模型 → AI 模型 API Key）",
          any(x["section"] == "AI模型" and x["section_ok"] and
              x["section_heading"] == "AI 模型 API Key" for x in men4), men4)
    check("[标签] 前缀也算同一节", any(x["section"] == "看图优先本地模型" and x["section_ok"]
                                      for x in men4), men4)

    print("②b 小节级双向引用：反向链接按「引的是哪一节」分组")
    r3 = call("/api/files", "POST", {"name": "zz-小节-引用方2.md",
                                     "content": "# 引用方2\n\n见 @%s#P3 那节，还有 @%s#不存在节 那节。\n"
                                                % (TGT, TGT)})[1]["file_id"]
    made.append(r3)
    tf = call("/api/files/" + tgt)[1]["file"]
    groups = tf.get("backlink_groups") or []
    g2 = next((g for g in groups if g["section"] == "P2"), None)
    g3 = next((g for g in groups if g["section"] == "P3"), None)
    gbad = next((g for g in groups if g["section"] == "不存在节"), None)
    check("P2 单独成组且 ok", bool(g2) and g2["count"] == 1 and g2["ok"] is True, groups)
    check("组里给出小节标题", bool(g2) and g2["heading"].startswith("P2"), g2 and g2["heading"])
    check("P3 也在", bool(g3) and g3["count"] == 1, g3)
    check("不存在的小节成组并标 ok=false", bool(gbad) and gbad["ok"] is False, gbad)
    men3 = call("/api/files/" + r3)[1]["file"].get("mentions") or []
    check("正向 mentions 带小节有效性",
          any(x["section"] == "不存在节" and x["section_ok"] is False for x in men3), men3)

    print("②b2 两端精确寻址：按目标端小节精确反查")
    bl = call("/api/files/%s/backlinks?section=P2" % tgt)[1]
    check("精确反查只回 P2 的引用",
          len(bl["backlinks"]) == 1 and bl["backlinks"][0]["id"] == ref, bl["backlinks"])
    check("精确反查带回规范小节名",
          (bl["backlinks"][0].get("section_heading") or "").startswith("P2"),
          bl["backlinks"][0].get("section_heading"))
    bl0 = call("/api/files/%s/backlinks?section=%s" % (tgt, urllib.parse.quote("没这一节")))[1]
    check("查不到的小节返回空（不硬凑）", bl0["backlinks"] == [], bl0["backlinks"])

    print("②b3 小节改名但前缀没变 → 引用端一个字不用改，b_section 自动跟随")
    st, sv = call("/api/files/" + tgt, "PUT",
                  {"content": TARGET.replace("## P2 第二节", "## P2 第二节（改了名）")})
    check("保存响应里报了小节刷新",
          ((sv.get("saved") or {}).get("sections") or {}).get("updated", 0) >= 1,
          (sv.get("saved") or {}).get("sections"))
    tf2 = call("/api/files/" + tgt)[1]["file"]
    g2c = next((g for g in (tf2.get("backlink_groups") or []) if g["section"] == "P2"), None)
    check("分组标题跟到新名且仍 ok",
          bool(g2c) and g2c["ok"] is True and "改了名" in (g2c["heading"] or ""), g2c)
    body_r = call("/api/files/" + ref)[1]["file"]["content"]
    check("引用端正文没被动过", ("@%s#P2" % TGT) in body_r, body_r.strip().splitlines()[-1][:60])
    call("/api/files/" + tgt, "PUT", {"content": TARGET})

    print("②c 小节改名 → 旧引用变成「小节级死链」（会被标出来）")
    call("/api/files/" + tgt, "PUT", {"content": TARGET.replace("## P3 第三节", "## 四、已经不是 P3 了")})
    tb = call("/api/files/" + tgt)[1]["file"]
    g3b = next((g for g in (tb.get("backlink_groups") or []) if g["section"] == "P3"), None)
    check("P3 那组标成失效", bool(g3b) and g3b["ok"] is False, g3b)
    g2b = next((g for g in (tb.get("backlink_groups") or []) if g["section"] == "P2"), None)
    check("没动过的 P2 仍然 ok", bool(g2b) and g2b["ok"] is True, g2b)
    print("②d 人点名、机器改：把小节引用整体改写")
    st, rl = call("/api/files/%s/sections/relink" % tgt, "POST",
                  {"old": "P3", "new": "四、已经"})
    check("改链成功", st == 200 and rl.get("moved", 0) >= 1, rl)
    body3 = call("/api/files/" + r3)[1]["file"]["content"]
    check("引用端写成了新小节", ("@%s#四、已经" % TGT) in body3, body3.strip().splitlines()[-1][:70])
    tf3 = call("/api/files/" + tgt)[1]["file"]
    g4 = next((g for g in (tf3.get("backlink_groups") or []) if g["section"] == "四、已经"), None)
    check("分组挂到新小节且 ok", bool(g4) and g4["ok"] is True and g4["count"] == 1, g4)
    st, bad = call("/api/files/%s/sections/relink" % tgt, "POST", {"old": "P3", "new": "根本没这节"})
    check("改到不存在的小节要报错", st == 400, bad.get("error"))

    call("/api/files/" + tgt, "PUT", {"content": TARGET})

    print("③ 改名：小节要跟着保留")
    call("/api/files/%s/rename" % tgt, "POST", {"name": TGT + "V2.md"})
    body = call("/api/files/" + ref)[1]["file"]["content"]
    check("引用处改成了新名且小节还在", ("@%sV2#P2" % TGT) in body, body.strip().splitlines()[-1])

    print("④ 删除目标篇 → 引用标成不可用（按名字部分判）")
    call("/api/files/" + tgt, "DELETE")
    made.remove(tgt)
    f2 = call("/api/files/" + ref)[1]["file"]
    check("deleted_refs 报出这个名字", ("%sV2" % TGT) in (f2.get("deleted_refs") or []),
          f2.get("deleted_refs"))
    print("⑤ 保存时校验小节是否存在")
    t2 = call("/api/files", "POST", {"name": "zz-小节-校验.md",
                                     "content": "# 校验\n\n## 甲节\n\n正文。\n"})[1]["file_id"]
    r2 = call("/api/files", "POST", {"name": "zz-小节-校验方.md",
                                     "content": "# 引用方\n\n先空着。\n"})[1]["file_id"]
    made += [t2, r2]
    st, ok1 = call("/api/files/" + r2, "PUT",
                   {"content": "# 引用方\n\n见 @zz-小节-校验#甲节 那节。\n"})
    check("存在的小节：不报警", not ((ok1.get("saved") or {}).get("bad_sections") or []),
          (ok1.get("saved") or {}).get("bad_sections"))
    st, bad1 = call("/api/files/" + r2, "PUT",
                    {"content": "# 引用方\n\n见 @zz-小节-校验#乙节 那节。\n"})
    bs = (bad1.get("saved") or {}).get("bad_sections") or []
    check("不存在的小节：报警", any(x["section"] == "乙节" for x in bs), bs)

finally:
    print("⑥ 清场")
    for fid in made:
        call("/api/files/" + fid, "DELETE")
    import sqlite3
    c = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "data", "index.db"))
    c.execute("DELETE FROM tombstones WHERE display LIKE 'zz-%'")
    c.commit()
    c.close()
    after = call("/api/health")[1]["stats"]["files"]
    check("文件数复原", after == files_before, "%s → %s" % (files_before, after))

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
