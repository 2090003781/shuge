"""接口清单工具：生成 docs/openapi.json，并和 src/server.py 里实际实现的路由对账。

  python tools/api_spec.py            # 人看的清单（按权限档分组）
  python tools/api_spec.py --write    # 写 docs/openapi.json
  python tools/api_spec.py --check    # 只对账：实现了的都登记了吗？登记的都有实现吗？（不一致退出 1）
"""
from __future__ import annotations

import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "src"))

import apidoc  # noqa: E402

SERVER = os.path.join(HERE, "src", "server.py")
OUT = os.path.join(HERE, "docs", "openapi.json")
OUT_OPS = os.path.join(HERE, "docs", "ops.json")
PLATFORM_PERMS = os.path.join(os.path.dirname(HERE), "core", "internal", "api",
                              "permissions.go")

EXACT = re.compile(r'u\.path\s*==\s*"([^"]+)"')
SUB = re.compile(r'u\.path\.endswith\("([^"]+)"\)')
START = re.compile(r'u\.path\.startswith\("([^"]+)"\)')
FID = re.compile(r'u\.path\[len\("([^"]+)"\):-len\("([^"]+)"\)\]')
FIELD_GET = re.compile(r'(?:b|q|body|self\._body\(\))\.get\(\s*"([^"]+)"')
# 用变量读 key（例如 `for k in (...): b.get(k)`）——这种路由没法逐字段核对，整个跳过
FIELD_DYN = re.compile(r'(?:b|q|body|self\._body\(\))\.get\(\s*[^"\s)]')
FIELD_IDX = re.compile(r'\b(?:b|body)\[\s*"([^"]+)"\s*\]')
DO_DEF = re.compile(r"\n    def (do_[A-Z]+)\(self\):")
# 所有写接口都认的通用字段（也走 X-Writer 头），不进"逐接口字段"对账
GENERIC_FIELDS = {"writer"}


def implemented() -> set:
    """从 server.py 里抠出实际实现的路由（方法 + 路径模式）"""
    s = io.open(SERVER, encoding="utf-8").read()
    blocks = re.split(r"\n    def (do_[A-Z]+)\(self\):", s)
    out = set()
    for i in range(1, len(blocks), 2):
        meth = blocks[i].split("_", 1)[1]
        if meth not in ("GET", "POST", "PUT", "DELETE"):
            continue
        for chunk in re.split(r"\n            if ", blocks[i + 1]):
            exact = EXACT.search(chunk)
            starts = START.findall(chunk)
            subs = SUB.findall(chunk)
            if exact:
                out.add("%s %s" % (meth, exact.group(1)))
            elif starts:
                base = starts[0]
                prefix = base if base.endswith("/") else base + "/"
                if subs:
                    for sub in subs:
                        out.add("%s %s<id>%s" % (meth, prefix, sub))
                else:
                    out.add("%s %s<id>" % (meth, prefix))
    return out


def _route_of_line(ln: str, meth: str):
    """这一行在判哪个路由？返回 "GET /api/files/<id>/text" 这种键；不是路由行返回 None"""
    m = FID.search(ln)
    if m:
        return "%s %s<id>%s" % (meth, m.group(1), m.group(2))
    m = EXACT.search(ln)
    if m:
        return "%s %s" % (meth, m.group(1))
    st = START.search(ln)
    if st:
        su = SUB.search(ln)
        base = st.group(1)
        prefix = base if base.endswith("/") else base + "/"
        return "%s %s<id>%s" % (meth, prefix, su.group(1) if su else "")
    return None


def implemented_fields() -> dict:
    """{路由: 实现里真的读了的请求体/查询字段}（粗抽，够用来抓"文档写了、实现没读"这类错位）

    为什么要有它：`--check` 原来只比路由名，实测"53 个一致"却漏掉了
    article 的 `max_chars`（实现里其实叫 `max`）—— 登记错字段名等于给调用方挖坑。
    """
    s = io.open(SERVER, encoding="utf-8").read()
    out, dynamic, route, meth = {}, set(), None, None
    for ln in s.split("\n"):
        d = re.match(r"\s*def (do_[A-Z]+)\(self\):", ln)
        if d:
            meth = d.group(1).split("_", 1)[1]
            route = None
            continue
        if meth not in ("GET", "POST", "PUT", "DELETE"):
            continue
        r = _route_of_line(ln, meth)
        if r:
            route = r
            continue
        if route:
            for f in FIELD_GET.findall(ln) + FIELD_IDX.findall(ln):
                out.setdefault(route, set()).add(f)
            if FIELD_DYN.search(ln):
                dynamic.add(route)               # 循环里读 key → 这个路由不逐字段核对
    return out, dynamic


def field_diff() -> dict:
    """字段级对账：登记的字段实现里读了吗？实现里读的字段登记了吗？"""
    impl, dyn = implemented_fields()
    doc = {("%s %s" % (e["m"], e["p"])): set(list((e.get("q") or {}).keys())
                                           + list((e.get("b") or {}).keys()))
           for e in apidoc.ENTRIES}
    undoc, unimpl, skipped = [], [], []
    for route, fields in sorted(impl.items()):
        got = {f for f in fields if f not in GENERIC_FIELDS}
        if not got or route in dyn:
            skipped.append(route)                    # 抽不出字段 / 循环里读 key → 不判
            continue
        want = doc.get(route)
        if want is not None:
            want = set(want) - GENERIC_FIELDS      # 通用字段两边都不判（如 writer：走头也走 body）
        if want is None:
            continue                                 # 路由名对不上由 route 那一层报
        for f in sorted(got - want):
            undoc.append((route, f))
        for f in sorted(want - got):
            unimpl.append((route, f))
    return {"undocumented": undoc, "unimplemented": unimpl, "skipped": sorted(skipped)}


def diff() -> dict:
    want = apidoc.paths()
    got = implemented()
    return {"missing": sorted(got - want), "extra": sorted(want - got),
            "implemented": sorted(got), "documented": sorted(want)}


def openapi() -> dict:
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "枢阁 库接口", "version": "1.0.0",
                 "description": "枢阁 / 库的本地 HTTP 接口。全部返回 JSON；异常返回 "
                                "{ok:false, error}；只监听 127.0.0.1。"},
        "servers": [{"url": "http://127.0.0.1:8848"}],
        "paths": {},
    }
    for e in apidoc.ENTRIES:
        p = e["p"].replace("<id>", "{id}")
        item = spec["paths"].setdefault(p, {})
        op = {"summary": e["sum"], "tags": [e.get("tier") or "read"],
              "responses": {"200": {"description": "成功"},
                            "4XX": {"description": "{ok:false, error}"}}}
        params = []
        for name, desc in (e.get("q") or {}).items():
            typ = "integer" if desc.startswith("int") else (
                "boolean" if desc.startswith("bool") else "string")
            params.append({"name": name, "in": "query", "required": "必填" in desc,
                           "schema": {"type": typ}, "description": desc})
        if "<id>" in e["p"]:
            params.append({"name": "id", "in": "path", "required": True,
                           "schema": {"type": "string"}, "description": "文件/文件夹 id"})
        if params:
            op["parameters"] = params
        if e.get("b"):
            props = {}
            for name, desc in e["b"].items():
                typ = "integer" if desc.startswith("int") else (
                    "boolean" if desc.startswith("bool") else (
                        "array" if desc.startswith("list") else "string"))
                props[name] = {"type": typ, "description": desc}
            op["requestBody"] = {"required": True, "content": {"application/json": {
                "schema": {"type": "object", "properties": props,
                           "required": [k for k, v in e["b"].items() if "必填" in v]}}}}
        item[e["m"].lower()] = op
    return spec


def platform_diff() -> dict:
    """把平台侧的 `OpCatalog`（agent 面）拿来对账：路由要在库目录里、档位要一致。

    平台那 38 条是**另一套命名**（`brain.*`）指向同一批路由 —— 两边各写一遍必然会漂，
    所以让库这边当唯一真源，`--check` 每次对一遍（2026-10-04 定的）。
    """
    out = {"found": False, "missing_route": [], "tier_mismatch": [], "count": 0}
    if not os.path.isfile(PLATFORM_PERMS):
        return out
    out["found"] = True
    txt = io.open(PLATFORM_PERMS, encoding="utf-8").read()
    m = re.search(r"var OpCatalog = \[\]OpEntry\{(.*?)\n\}", txt, re.S)
    if not m:
        return out
    lib = {("%s %s" % (e["m"], e["p"])): (e.get("tier") or "read") for e in apidoc.ENTRIES}
    lib = {k.replace("<id>", "{id}"): v for k, v in lib.items()}
    for oid, lvl, route, _label in re.findall(
            r'\{"(brain\.[a-z_]+)",\s*"(read|write|trust)",\s*"([^"]+)",\s*"([^"]+)"\}', txt):
        out["count"] += 1
        r = route.strip()
        want = "trust" if lvl == "danger" else lvl
        if r not in lib:
            out["missing_route"].append("%s → %s（库里没有这条路由）" % (oid, r))
        elif lib[r] != want:
            out["tier_mismatch"].append("%s → %s：平台说 %s，库里登记 %s" % (oid, r, want, lib[r]))
    return out


def main() -> int:
    args = set(sys.argv[1:])
    d = diff()
    if "--check" in args:
        bad = []
        for x in d["missing"]:
            bad.append("实现但没登记：%s" % x)
        for x in d["extra"]:
            bad.append("登记了但没实现：%s" % x)
        fd = field_diff()
        for route, f in fd["undocumented"]:
            bad.append("字段没登记：%s 里的 %s" % (route, f))
        for route, f in fd["unimplemented"]:
            bad.append("字段登记了但实现里没读：%s 里的 %s" % (route, f))
        # ① 档位 / 层次：每条都得有（规范里这两样是必填）
        for x in apidoc.catalog():
            if x["tier"] not in ("read", "write", "trust"):
                bad.append("档位不认识（只能是 read/write/trust）：%s → %s" % (x["id"], x["tier"]))
            if x["kind"] not in ("data", "engine", "host", "ops"):
                bad.append("层次不认识（只能是 data/engine/host/ops）：%s → %s" % (x["id"], x["kind"]))
        # ② 批量白名单必须指向真存在、真实现的路由
        for op, spec in apidoc.BATCH_OPS.items():
            key = "%s %s" % (spec["method"], spec["route"].replace("{id}", "<id>"))
            if key not in apidoc.paths():
                bad.append("批量白名单指向没登记的路由：%s → %s" % (op, key))
            if key not in d["implemented"]:
                bad.append("批量白名单指向没实现的路由：%s → %s" % (op, key))
        # ③ 跟平台 OpCatalog 对账
        pd = platform_diff()
        for x in pd["missing_route"]:
            bad.append("平台操作目录里有库里没有的路由：%s" % x)
        for x in pd["tier_mismatch"]:
            bad.append("档位不一致：%s" % x)
        if bad:
            for b in bad:
                print("❌ " + b)
            return 1
        pd = platform_diff()
        kinds = {}
        for x in apidoc.catalog():
            kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1
        print("✅ 操作目录与实现一致：%d 个路由 · 字段级也对得上（跳过 %d 个抽不出字段的路由）"
              % (len(d["implemented"]), len(fd["skipped"])))
        print("   档位：%s ｜ 层次：%s ｜ 可批量：%d 条"
              % (", ".join("%s=%d" % (t, len(apidoc.by_tier(t)))
                           for t in ("read", "write", "trust")),
                 ", ".join("%s=%d" % kv for kv in sorted(kinds.items())),
                 len(apidoc.BATCH_OPS)))
        print("   平台 OpCatalog：%s"
              % ("对上了（%d 条）" % pd["count"] if pd["found"] else "没找到 permissions.go，跳过"))
        return 0
    if "--write" in args:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        spec = openapi()
        io.open(OUT, "w", encoding="utf-8", newline="\n").write(
            json.dumps(spec, ensure_ascii=False, indent=1) + "\n")
        ops = {"真源": "src/apidoc.py",
               "字段": ["id", "m", "p", "tier", "kind", "batch", "sum", "q", "b"],
               "档位": {"read": "只读", "write": "可写", "trust": "相信"},
               "层次": {"data": "库内数据", "engine": "格式引擎", "host": "宿主能力", "ops": "运维"},
               "批量入口": "POST /api/batch {op, ids, args}",
               "批量白名单": apidoc.BATCH_OPS,
               "操作": apidoc.catalog()}
        io.open(OUT_OPS, "w", encoding="utf-8", newline="\n").write(
            json.dumps(ops, ensure_ascii=False, indent=1) + "\n")
        print("✅ 已写 %s（%d 条路径）与 %s（%d 条操作）"
              % (os.path.relpath(OUT, HERE), len(spec["paths"]),
                 os.path.relpath(OUT_OPS, HERE), len(ops["操作"])))
        return 0
    kind_cn = {"data": "库内数据", "engine": "格式引擎", "host": "宿主能力", "ops": "运维"}
    for tier, label in (("read", "只读"), ("write", "可写（可撤销）"),
                        ("trust", "相信（默认不给 agent）")):
        print("── %s ──" % label)
        for e in sorted(apidoc.by_tier(tier), key=lambda x: (apidoc.kind_of(x["p"]), x["p"])):
            key = "%s %s" % (e["m"], e["p"])
            isb = any(v["route"].replace("{id}", "<id>") == e["p"] and v["method"] == e["m"]
                      for v in apidoc.BATCH_OPS.values())
            print("   %-6s %-38s [%s]%s %s"
                  % (e["m"], e["p"], kind_cn[apidoc.kind_of(e["p"])],
                     " 可批量" if isb else "      ", e["sum"]))
    if d["missing"] or d["extra"]:
        print("\n⚠️ 和实现对不上（跑 --check 看细节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
