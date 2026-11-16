#!/usr/bin/env python3
"""命名包工具的离线自测（用临时目录当 CONTEXT_DIR/PACKS_FILE，不碰线上清单、不写平台账）。"""
import importlib.util
import json
import os
import sys
import tempfile

PLUG = r"D:\core\plugins\context_pack_plugin.py"
TMP = tempfile.mkdtemp(prefix="packtest-")
os.environ["CONTEXT_DIR"] = TMP
os.environ["PACKS_FILE"] = os.path.join(TMP, "packs.json")
os.environ["VAULT_PATH"] = r"D:\个人数据\辞玖"

spec = importlib.util.spec_from_file_location("ctxpack", PLUG)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
# 隔离：阻断真实组包 / 真实监听 / 真实上报。
# 上报必须打桩——早期版本直接发到线上 8790，既往真账本写测试账，又因为没有 token
# 在平台自检里留下一条"未鉴权调用被拒"的 warn（实测踩到）。
sent = []
m.refresh_once = lambda **kw: 0
m.restart_source_watchers = lambda: None
m.http_post = lambda path, payload: (sent.append((path, payload)), {})[1]
m.http_get = lambda path: [{"agent": "agent.dsh", "enabled": True, "docs": ["profile"]},
                          {"agent": "agent.reasonix", "enabled": True, "docs": ["profile"]}]


def last_event(etype):
    """取出最后一次上报的事件（看类型与 payload）"""
    for path, payload in reversed(sent):
        ev = (payload or {}).get("event") or {}
        if ev.get("type") == etype:
            return ev
    return None

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  ← " + str(extra)))
    if not cond:
        fails.append(name)


F1 = "项目/枢阁/待办清单.md"
F2 = "项目/枢阁/日志.md"

check("初始清单为空", m.load_packs() == [])

# 1. 文件不存在 → 拒绝并报出是哪个
try:
    m._tool_pack_create({"name": "坏包", "files": [F1, "项目/不存在.md"]})
    check("缺文件应被拒", False, "没抛错")
except ValueError as e:
    check("缺文件应被拒", "项目/不存在.md" in str(e), e)

# 2. 空名 → 拒绝
try:
    m._tool_pack_create({"name": "   ", "files": [F1]})
    check("空名应被拒", False, "没抛错")
except ValueError as e:
    check("空名应被拒", "包名" in str(e), e)

# 3. 路径穿越 → 拒绝
try:
    m._tool_pack_create({"name": "穿越", "files": ["../../etc/passwd"]})
    check("路径穿越应被拒", False, "没抛错")
except ValueError as e:
    check("路径穿越应被拒", ("不可用" in str(e)) or ("找不到" in str(e)), e)

# 4. 正常新建
r = m._tool_pack_create({"name": "测试包", "files": [F1], "agents": ["agent.dsh"]})
pid = r["pack"]["id"]
check("新建成功", r["pack"]["name"] == "测试包" and m.paths_of(r["pack"]["files"]) == [F1], r)
check("落盘可读回", len(m.load_packs()) == 1)
check("临时目录里生成 packs.json", os.path.isfile(os.path.join(TMP, "packs.json")))

# 5. 去重
r2 = m._tool_pack_create({"name": "重复文件", "files": [F1, F1, F2], "agents": ["agent.dsh"]})
check("文件去重", m.paths_of(r2["pack"]["files"]) == [F1, F2], r2["pack"]["files"])

# 13. 单文件级启停（2026-09-13 加）：停用 = 条目保留、不进包
sf = m._tool_pack_set_file({"kind": "pack", "id": r2["pack"]["id"], "path": F1, "enabled": False})
check("单文件停用返回条目", m.paths_of(sf["files"]) == [F1, F2], sf["files"])
check("停用态被记住", [e["enabled"] for e in sf["files"]] == [False, True], sf["files"])
# 只看**被停用的那个包**：它里面的 F1 应显"已停用"、且正文不进包
pack_only = m.build_pack("agent.dsh", ["profile"], [p for p in m.packs_for_agent("agent.dsh")
                                                   if p["id"] == r2["pack"]["id"]])
check("停用文件正文不进包（留的是标记不是正文）", ("### " + F1 + "\n(已停用，未纳入)") in pack_only,
      pack_only[-400:])
_saved = [p for p in m.load_packs() if p["id"] == r2["pack"]["id"]][0]["files"]
check("停用条目仍在清单里（可再启用）", _saved[0]["enabled"] is False and len(_saved) == 2, _saved)
sf2 = m._tool_pack_set_file({"kind": "pack", "id": r2["pack"]["id"], "path": F1, "enabled": True})
check("重新启用后正文回来", "待办" in m.build_pack("agent.dsh", ["profile"], m.packs_for_agent("agent.dsh")))
try:
    m._tool_pack_set_file({"kind": "pack", "id": r2["pack"]["id"], "path": "项目/没有.md", "enabled": False})
    check("改不存在的条目应报错", False, "没抛错")
except ValueError as e:
    check("改不存在的条目应报错", "没有这个文件" in str(e), e)

# 14. 路由文档清单：读默认 → 改写覆盖 → 写入 routes-docs.json → 组包生效
en0 = m._tool_pack_entries({"agent": "agent.dsh"})
check("默认路由文档来自平台", m.paths_of(en0["files"]) == ["profile"] and en0["overridden"] is False, en0)
en1 = m._tool_pack_entries({"agent": "agent.dsh", "files": [{"path": "profile", "enabled": True},
                                                           {"path": F1, "enabled": False},
                                                           {"path": F2, "enabled": True}]})
check("路由文档改写成功", en1["overridden"] is True and len(en1["files"]) == 3, en1)
check("路由覆盖已落盘", os.path.isfile(os.path.join(TMP, "routes-docs.json")))
check("覆盖后读回一致", len(m._tool_pack_entries({"agent": "agent.dsh"})["files"]) == 3)
pack_r = m.build_pack("agent.dsh", m.load_route_docs()["agent.dsh"], [])
check("路由文档停用项被跳过（无该小节）", ("## " + F1) not in pack_r and ("### " + F1) not in pack_r,
      pack_r[:300])
check("路由文档启用项仍进包", ("## " + F2) in pack_r, pack_r[:300])

# 15. 路由文档：关键字与文件路径都合法；非法条目被拒
try:
    m._tool_pack_entries({"agent": "agent.dsh", "files": [{"path": "人设/没有.md", "enabled": True}]})
    check("非法路由条目应被拒", False, "没抛错")
except ValueError as e:
    check("非法路由条目应被拒", ("不可用" in str(e)) or ("不在 Vault" in str(e)) or ("关键字" in str(e)), e)
check("关键字仍可用", m._tool_pack_entries({"agent": "agent.dsh", "files": ["rules-l1"]})["files"][0]["path"] == "rules-l1")
check("缺 agent 参数应报错", True)  # 占位：见下一条真断言
try:
    m._tool_pack_entries({})
    check("缺 agent 应被拒", False, "没抛错")
except ValueError as e:
    check("缺 agent 应被拒", "agent" in str(e), e)

# 6. 组包：内容真的进了包
pack = m.build_pack("agent.dsh", ["profile"], m.packs_for_agent("agent.dsh"))
check("包里有命名包标题", "📎 测试包" in pack and "📎 重复文件" in pack, pack[:200])
check("包里有文件路径小节", ("### " + F1) in pack)
check("包里有文件正文片段", "待办" in pack)
check("头部列出命名包", "命名包：测试包" in pack)
check("没命中该 agent 的包不进包", m.packs_for_agent("agent.reasonix") == [], m.packs_for_agent("agent.reasonix"))

# 7. 预览
pv = m._tool_pack_preview({"files": [F1, "项目/不存在.md"]})
check("预览返回两条", len(pv["files"]) == 2, pv)
check("预览坏文件标记 ok=False", pv["files"][1]["ok"] is False and pv["files"][1]["err"])

# 8. 改名 + 关开关 + 换 agent
u = m._tool_pack_update({"id": pid, "name": "改名后", "enabled": False})
check("改名生效", u["pack"]["name"] == "改名后")
check("关开关后不参与组包", m.packs_for_agent("agent.dsh") == [p for p in m.load_packs() if p["id"] != pid],
      m.packs_for_agent("agent.dsh"))
u = m._tool_pack_update({"id": pid, "enabled": True, "agents": []})
check("空 agents = 所有 agent", len(m.packs_for_agent("agent.reasonix")) == 1, m.packs_for_agent("agent.reasonix"))

# 9. 删
d = m._tool_pack_delete({"id": pid})
check("删除成功", d["deleted"] == pid and len(m.load_packs()) == 1, d)
try:
    m._tool_pack_delete({"id": pid})
    check("重复删应报错", False, "没抛错")
except ValueError as e:
    check("重复删应报错", "没有这个包" in str(e), e)

# 10. 上限
try:
    m._tool_pack_create({"name": "太多", "files": [F1] * (m.MAX_PACK_FILES + 1)})
    check("超文件数上限应被拒", False, "没抛错")
except ValueError as e:
    check("超文件数上限应被拒", "最多" in str(e), e)

# 11. 坏清单文件 → 当作空（不炸）
with open(os.path.join(TMP, "packs.json"), "w", encoding="utf-8") as f:
    f.write("{坏 json")
check("坏清单当作空", m.load_packs() == [])
check("总内容上限常量存在", m.PACK_TOTAL_CHARS == 40000)

# 12. 包变更留痕：新建/删除都要发 platform.context.pack-changed（带 op 与包 id）
ev = last_event("platform.context.pack-changed")
check("包变更上报了 pack-changed", ev is not None, sent[-1] if sent else None)
check("变更事件带 op=delete", ev and ev["payload"].get("op") == "delete", ev and ev["payload"])
check("变更事件带包 id", ev and ev["payload"].get("pack_id") == pid, ev and ev["payload"])
check("变更事件带被删包名（不是 null，事后查得到删了啥）", ev and ev["payload"].get("name"), ev and ev["payload"])
check("没有落到真平台（上报全被打桩）", all(p == "/api/bridge/send" for p, _ in sent), sent)



# 16. 外部绝对路径（Vault 之外）——2026-09-13 加
EXT = r"D:\core\plugins\context_pack_plugin.py"   # 用一个确实存在的外部文件当样本
check("is_abs_path 识别绝对路径", m.is_abs_path(EXT) and not m.is_abs_path(F1))
_, err = m.resolve_path(EXT)
check("外部文件可解析", err == "", err)
check("外部文件不存在应报错", m.resolve_path(r"D:\\no\\such\\file.md")[1] != "")
check("拒读名单生效（.env）", "拒读" in m.resolve_path(r"D:\\x\\.env")[1])
check("拒读名单生效（token.json）", "拒读" in m.resolve_path(r"D:\\x\\token.json")[1])
okx, missx = m.validate_files([EXT])
check("validate_files 接受外部绝对路径", len(okx) == 1 and not missx, (okx, missx))
okx2, missx2 = m.validate_files([r"D:\\no\\such.md"])
check("外部不存在应进 missing", len(missx2) == 1 and not okx2, (okx2, missx2))
okx3, missx3 = m.validate_files(["项目/不存在.md"])
check("相对不存在仍应进 missing（回归）", len(missx3) == 1 and not okx3, (okx3, missx3))
r_ext = m._tool_pack_create({"name": "外部文件包", "files": [EXT], "agents": ["agent.dsh"]})
check("含外部文件的包可创建", m.paths_of(r_ext["pack"]["files"]) == [EXT], r_ext["pack"]["files"])
pack_ext = m.build_pack("agent.dsh", [], [p for p in m.load_packs() if p["id"] == r_ext["pack"]["id"]])
check("外部文件正文进包", "上下文包插件（external）" in pack_ext, pack_ext[:200])   # 截断后仍应含开头内容
m._tool_pack_delete({"id": r_ext["pack"]["id"]})


check("拒读名单文案可区分", "拒读名单" in m.missing_reason_text([r"D:\\x\\.env"]))
check("不存在文案可区分", "文件不存在" in m.missing_reason_text([r"D:\\no\\x.md"]))
print("\n结果：" + ("全部通过" if not fails else f"{len(fails)} 项失败 → {fails}"))
sys.exit(1 if fails else 0)

