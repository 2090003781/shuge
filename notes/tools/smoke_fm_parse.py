# -*- coding: utf-8 -*-
"""前置属性解析的容错验收（纯本地：不连库、不起服务）。

由来（2026-09-27 审计）：辞玖笔记里 `关联: @某篇 [[规则库]]` 这种值以 YAML 保留指示符（@ [ ）开头，
PyYAML 直接报错；老代码把"解析失败"当成"没有前置属性" → 整块丢掉，**还顺手把库里已有的属性清空、
把上次由 YAML 带进来的标签撤掉**。实测当时库里 220 篇带前置属性，7 篇就是这么变空的。

现在三级兜底：原样解析 → 给危险裸值加引号再解析 → 行级朴素解析；真的一点都读不出来时保持原样。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import frontmatter as fm  # noqa: E402

PASS = 0
FAIL = []


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
    print("  %s %s%s" % ("✅" if cond else "❌", name, "" if cond else "  | " + str(extra)[:220]))
    if not cond:
        FAIL.append(name)


class FakeDB:
    """够 apply() 用的最小替身：只记账，不碰真库"""

    def __init__(self, frontmatter=None, tags=None):
        self.fm = dict(frontmatter or {})
        self.tags = list(tags or [])
        self.writes = []
        self.conn = None

    def frontmatter_of(self, fid):
        return dict(self.fm)

    def set_frontmatter(self, fid, raw):
        self.writes.append(raw)
        self.fm = raw

    def tags_of(self, fid):
        return list(self.tags)

    def add_tag(self, fid, t):
        if t not in self.tags:
            self.tags.append(t)

    def remove_tag(self, fid, t):
        if t in self.tags:
            self.tags.remove(t)

    def file_by_id(self, fid):
        return None


def fm_of(db):
    """FakeDB 存进去的是 JSON 文本；断言前先还原成 dict"""
    return json.loads(db.fm) if isinstance(db.fm, str) else dict(db.fm or {})


def main():
    print("① 值以 @ / [[ ]] 开头时不能整块丢（辞玖笔记里最常见的两种写法）")
    t1 = "---\ntype: 思维协议\n关联: @唯稳律研究笔记 [[规则库]] @错误库\ntags:\n  - 认知架构\n---\n正文\n"
    m1 = fm.parse(t1)
    check("键都在（type / 关联 / tags）", m1.get("type") == "思维协议" and "关联" in m1 and "tags" in m1, m1)
    check("值是原文：@ 与 [[ ]] 都留着", m1.get("关联") == "@唯稳律研究笔记 [[规则库]] @错误库", m1.get("关联"))
    check("块列表标签读出来了", m1.get("tags") == ["认知架构"], m1.get("tags"))

    t2 = "---\ntags: [缓存, ai-notes, @引用, 路径引用]\nupdated: 2026-09-16\n---\n正文\n"
    m2 = fm.parse(t2)
    check("行内列表里混着 @元素 也不炸", m2.get("tags") == ["缓存", "ai-notes", "@引用", "路径引用"], m2.get("tags"))

    t3 = "---\n关联: [[Reasonix-人设配置]] [[规则库]]\ncreated: 2026-08-16\n---\n正文\n"
    m3 = fm.parse(t3)
    check("值以 [[ 开头也不炸", m3.get("关联") == "[[Reasonix-人设配置]] [[规则库]]", m3.get("关联"))
    check("同一块里的日期还能进 doc_date", fm.date_of(m3) == "2026-08-16", fm.date_of(m3))
    check("中文键「日期」不在识别范围（如实）", fm.date_of({"日期": "2026-08-16"}) == "", fm.date_of({"日期": "2026-08-16"}))

    t4 = "---\ntitle: 正常\nstatus: active\ntags: [a, b]\n---\n正文\n"
    check("本来就正常的 YAML 解出来一字不差",
          fm.parse(t4) == {"title": "正常", "status": "active", "tags": ["a", "b"]}, fm.parse(t4))
    check("没有前置块时照旧返回空", fm.parse("没有块\n") == {} and fm.split("没有块\n")[1] == [])

    print("② 写回去要带引号，往返不丢")
    out = fm.upsert("---\ntitle: x\n---\nbody\n", {"关联": "@甲 [[乙]]"})
    check("危险值被引起来", '关联: "@甲 [[乙]]"' in out.splitlines(), out.splitlines())
    check("再解析回来还是原值", fm.parse(out).get("关联") == "@甲 [[乙]]", fm.parse(out))
    out2 = fm.upsert("---\ntags:\n  - 旧\n---\nbody\n", {"tags": ["甲", "乙"]})
    check("块列表改写成行内列表且能解析", fm.norm_tags(fm.parse(out2).get("tags")) == ["甲", "乙"], out2)

    print("③ 真的一点都解析不出来时：不清空已有属性、不撤标签（老代码就是在这儿清了 7 篇）")
    bad = "---\n: 这一行没有键\n- 只有列表项\n---\n正文\n"
    db = FakeDB(frontmatter={"title": "旧标题", "_tags_applied": ["甲"]}, tags=["甲"])
    res = fm.apply(db, "F", bad)
    check("没把 frontmatter 写成空", db.writes == [] or all(w != "" for w in db.writes), db.writes)
    check("旧属性保住了", fm_of(db).get("title") == "旧标题", db.fm)
    check("标签没被撤", db.tags == ["甲"], db.tags)
    check("返回值里如实报了 parse_error", bool(res.get("parse_error")), res)

    print("④ 正常块仍然照旧覆盖（别把容错做成「永不更新」）")
    db2 = FakeDB(frontmatter={"_tags_applied": ["旧标签"]}, tags=["旧标签"])
    fm.apply(db2, "F", "---\ntitle: 新\ntags: [新标签]\n---\n正文\n")
    check("属性被覆盖成新的", "新" in str(db2.writes[-1]) and "旧标题" not in str(db2.writes[-1]), db2.writes[-1])
    check("加了 YAML 里的新标签", "新标签" in db2.tags, db2.tags)
    check("撤了上次由 YAML 带进来的旧标签", "旧标签" not in db2.tags, db2.tags)

    print("⑤ 补救成功的块不留 parse_error（只是把危险值当字符串读，不是坏块）")
    db3 = FakeDB()
    res3 = fm.apply(db3, "F", t1)
    check("按补救解析出来的属性进库了", "关联" in str(db3.writes[-1]), str(db3.writes[-1])[:120])
    check("parse_error 为空", not res3.get("parse_error"), res3)

    print("\n========  结论: %d 通过 / %d 失败" % (PASS, len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
