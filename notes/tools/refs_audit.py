"""老引用巡检：把库里"解析不出来的 @引用"和"没转干净的 [[旧引用]]"分类列清楚。

只读、不改库。输出人看的报告 + 机器可读的 JSON（data/reports/refs-audit-<时间>.json），
供决定"哪些值得改、怎么改"。`scan()` 也供 `tools/selfcheck.py` 复用。

分类（未解析的 @锚点）：
  PSEUDO     伪标签/包名（@event、@tool、@deepseek-ai/dsh…）—— 不是引用，建议进忽略名单
  AMBIGUOUS  末段重名（@日志、@README）—— 库里有这篇但没有唯一名，得补路径才能指准
  MISSING    库里确实没有这个名字（@四维认知架构）—— 只能等那篇出现，或本来就不是引用
  FRAGMENT   碎片（@{、@'）—— 噪点，不动
  LOGISH     只在带 logs 标签的日志文件里出现的名字（@架构喵、@python.exe…）—— 对话里的称呼，不是引用
  BADSEC     引用写了 `#小节`，但目标篇里没有这一节（小节改名/删掉之后就会这样）

`data/refs-ignore.txt` 里列出的锚点会单独统计（"已忽略"），不再混进上面几类。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "src"))
sys.path.insert(0, os.path.join(HERE, "tools"))

from db import Database, DEFAULT_DATA_DIR        # noqa: E402
from mentions import parse_refs                  # noqa: E402
import convert_refs as C                         # noqa: E402

PSEUDO = {
    "event", "tool", "status", "user_prompt", "start", "active", "stop", "echo", "param",
    "return", "throws", "deprecated", "override", "inject", "autowired", "bean", "component",
    "service", "repository", "restcontroller", "requestmapping", "getmapping", "postmapping",
    "test", "beforeeach", "aftereach", "pytest", "fixture", "dataclass", "property",
    "staticmethod", "classmethod", "media", "keyframes", "import", "supports", "apply",
}
SCOPED = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")      # @deepseek-ai/dsh 这类


def body_lines(text: str) -> str:
    """去掉代码围栏与行内代码里的内容（那里写的是示例，不是引用）"""
    out, fence = [], False
    for line in (text or "").splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if not fence:
            out.append(line)
    return C.CODE.sub(" ", "\n".join(out))


def classify(anchor: str, leaf_hits: dict, leaf_dup: set) -> str:
    a = (anchor or "").strip()
    if len(a) <= 2 or not re.search(r"[\w\u4e00-\u9fff]", a):
        return "FRAGMENT"
    core = a.split("#")[0]
    if core.lower() in PSEUDO:
        return "PSEUDO"
    if SCOPED.match(core) and not re.search(r"[\u4e00-\u9fff]", a):
        return "PSEUDO"
    leaf = core.rsplit("/", 1)[-1]
    if leaf in leaf_dup or leaf_hits.get(leaf):
        return "AMBIGUOUS"
    return "MISSING"


def load_ignore(data_dir: str) -> set:
    """读忽略名单（data/refs-ignore.txt）：伪标签/包名/人名，不算"要人看的引用问题" """
    ignore = set()
    ipath = os.path.join(data_dir, "refs-ignore.txt")
    if os.path.isfile(ipath):
        for line in io.open(ipath, encoding="utf-8").read().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                ignore.add(line)
    return ignore


def scan(db: Database, data_dir: str) -> dict:
    """扫一遍全库的引用并分类（只读）—— 巡检报告与 selfcheck 共用"""
    look = db.ref_resolver()
    resolve_c, display_c = C.build_resolver(db)

    leaf_hits = defaultdict(list)
    for r in db.conn.execute("SELECT id, name FROM files"):
        nm = r["name"] or ""
        d = nm[:-3] if nm.lower().endswith(".md") else nm
        leaf_hits[d].append(r["id"])
    leaf_dup = {k for k, v in leaf_hits.items() if len(v) > 1}

    groups, where = defaultdict(Counter), defaultdict(Counter)
    seen_in = defaultdict(set)
    bad_sec, bad_sec_where = Counter(), {}
    wiki_left, wiki_where = Counter(), {}
    unquoted_space = []
    scanned = 0

    for r in db.conn.execute("SELECT id, name FROM files"):
        try:
            text = db.get_content(r["id"]).decode("utf-8", "replace")
        except Exception:
            continue
        scanned += 1
        body = body_lines(text)
        for ref in parse_refs(body):
            fid = look(ref["name"])
            if not fid:
                where[ref["anchor"]][r["name"]] += 1
                groups[classify(ref["anchor"], leaf_hits, leaf_dup)][ref["anchor"]] += 1
                seen_in[ref["anchor"]].add(r["id"])
            elif ref["section"] and not db.section_match(fid, ref["section"]):
                bad_sec[ref["anchor"]] += 1
                bad_sec_where.setdefault(ref["anchor"], r["name"])
        for m in C.WIKI.finditer(body):                      # 还没转干净的旧格式（围栏外）
            t0 = m.group(1).strip()
            if t0 and not resolve_c(t0)[0]:
                wiki_left[t0[:60]] += 1
                wiki_where.setdefault(t0[:60], r["name"])
        st = Counter()
        if C.fix_spaces_text(text, look, st) != text:        # 名字带空格没加引号
            unquoted_space.append((r["name"], st["fixed"]))

    # 只在 logs 标签文件里出现的 → 日志噪声（对话里的称呼，不是引用）
    log_ids = {r["file_id"] for r in db.conn.execute(
        "SELECT ft.file_id FROM file_tags ft JOIN tags t ON t.id=ft.tag_id WHERE t.name='logs'")}
    ignore = load_ignore(data_dir)
    for anchor in list(groups.get("MISSING", {})) + list(groups.get("PSEUDO", {})):
        if seen_in[anchor] and seen_in[anchor] <= log_ids:
            groups["LOGISH"][anchor] += (groups["MISSING"].pop(anchor, 0)
                                         + groups["PSEUDO"].pop(anchor, 0))
    ignored = Counter()
    for kind in list(groups):
        for anchor in list(groups[kind]):
            if anchor in ignore:
                ignored[anchor] += groups[kind].pop(anchor)

    total = sum(sum(c.values()) for c in groups.values())
    need_human = {k: sum(c.values()) for k, c in groups.items()
                  if k in ("AMBIGUOUS", "MISSING")}
    return {
        "scanned_files": scanned, "unresolved_total": total,
        "groups": {k: dict(v.most_common()) for k, v in groups.items()},
        "counts": {k: sum(v.values()) for k, v in groups.items()},
        "bad_sections": sum(bad_sec.values()), "bad_sections_detail": dict(bad_sec.most_common()),
        "bad_sec_where": bad_sec_where,
        "wiki_left": sum(wiki_left.values()), "wiki_left_detail": dict(wiki_left.most_common()),
        "wiki_where": wiki_where,
        "ignored": sum(ignored.values()), "ignored_detail": dict(ignored.most_common()),
        "unquoted_space": unquoted_space,
        "need_human": need_human,
        "where": {k: dict(v.most_common(5)) for k, v in where.items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="老引用巡检（只读）")
    ap.add_argument("--data", default=None)
    ap.add_argument("--json", default="", help="JSON 报告写到哪里（默认 data/reports/…）")
    ap.add_argument("--top", type=int, default=12, help="每类列多少个例子")
    a = ap.parse_args()
    data_dir = a.data or DEFAULT_DATA_DIR
    db = Database(data_dir)
    R = scan(db, data_dir)
    groups, where = R["groups"], R["where"]

    print("扫描 %d 篇；未解析的 @引用 %d 处（%d 个不同锚点）"
          % (R["scanned_files"], R["unresolved_total"], sum(len(c) for c in groups.values())))
    for kind in ("AMBIGUOUS", "MISSING", "LOGISH", "PSEUDO", "FRAGMENT"):
        c = Counter(groups.get(kind) or {})
        print("\n【%s】%d 处 / %d 个锚点" % (kind, sum(c.values()), len(c)))
        for anchor, cnt in c.most_common(a.top):
            ex = "、".join(list(where.get(anchor, {}))[:2])
            print("   %-38s x%-4d 例：%s" % (anchor[:38], cnt, ex[:56]))
    bs = Counter(R["bad_sections_detail"])
    print("\n【引用的小节不存在】%d 处 / %d 个锚点" % (sum(bs.values()), len(bs)))
    for anchor, cnt in bs.most_common(a.top):
        print("   %-38s x%-4d 例：%s" % (anchor[:38], cnt, R["bad_sec_where"].get(anchor, "")[:40]))
    wl = Counter(R["wiki_left_detail"])
    print("\n【未转干净的 [[旧引用]]】%d 个锚点" % len(wl))
    for anchor, cnt in wl.most_common(a.top):
        print("   %-38s x%-4d 例：%s" % (anchor[:38], cnt, R["wiki_where"].get(anchor, "")[:40]))
    print("\n【名字带空格但没加引号】%d 篇（可用 --fix-spaces 自动补）" % len(R["unquoted_space"]))
    for nm, n in R["unquoted_space"][:a.top]:
        print("   %-40s %d 处" % (nm[:40], n))

    out = a.json or os.path.join(data_dir, "reports",
                                "refs-audit-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    io.open(out, "w", encoding="utf-8", newline="").write(json.dumps({
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "scanned_files": R["scanned_files"],
        "unresolved_total": R["unresolved_total"], "groups": groups, "examples": where,
        "wiki_left": R["wiki_left_detail"], "bad_sections": R["bad_sections_detail"],
        "ignored": R["ignored_detail"], "unquoted_space": R["unquoted_space"],
    }, ensure_ascii=False, indent=1))
    if R["ignored"]:
        print("\n【已按忽略名单跳过】%d 处 / %d 个锚点：%s"
              % (R["ignored"], len(R["ignored_detail"]), "、".join(list(R["ignored_detail"])[:8])))
    print("\n报告：%s" % out)
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
