"""文件/文件夹操作的端到端冒烟：新建文件夹 → 改名 → 移动 → 删空 → 事件齐 → 提及不断链。

自建自清：只在库里造 zz- 开头的临时物，测完删干净。
"""
import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
ok_all = True


def call(path, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:150] if extra else ""))


def events_after(gseq, types=""):
    q = "/api/events?after=%d&limit=200" % gseq
    if types:
        q += "&types=" + types
    return call(q)[1]["events"]


def find(evs, t):
    return [e for e in evs if e["type"] == t]


base = call("/api/events?limit=1")[1]["last_gseq"]
files_before = call("/api/health")[1]["stats"]["files"]
pend_before = call("/api/health")[1]["pending_vectors"]
made = {"files": [], "folders": []}

try:
    print("① 建两个文件夹")
    st, a = call("/api/folders", "POST", {"name": "zz-体检-A"})
    made["folders"].append(a["folder_id"])
    st, b = call("/api/folders", "POST", {"name": "zz-体检-B"})
    made["folders"].append(b["folder_id"])
    check("两个都建成功", st == 201 and a.get("folder_id") and b.get("folder_id"))
    st, fl = call("/api/folders")
    paths = [f["path"] for f in fl["folders"]]
    check("扁平表里有它们", "zz-体检-A" in paths and "zz-体检-B" in paths, paths[-3:])

    print("② 建文件 + 建立提及关系（引用另一篇）")
    st, tgt = call("/api/files", "POST", {"folder_id": a["folder_id"], "name": "zz-体检-目标.md",
                                         "content": "# 目标\n\n被引用的一篇。\n"})
    made["files"].append(tgt["file_id"])
    src_body = "# 引用方\n\n看 @zz-体检-目标 这篇。\n"
    st, src = call("/api/files", "POST", {"folder_id": a["folder_id"], "name": "zz-体检-引用方.md",
                                          "content": src_body})
    made["files"].append(src["file_id"])
    f = call("/api/files/" + src["file_id"])[1]["file"]
    check("边建好了（1 条 mention）", len(f["mentions"]) == 1, f["mentions"])

    print("③ 给被引用方改名 → 边跟着改，正文里还是旧名也能解析")
    st, r = call("/api/files/%s/rename" % tgt["file_id"], "POST", {"name": "zz-体检-目标改名.md"})
    check("改名接口 200", st == 200 and r["file"]["name"] == "zz-体检-目标改名.md", r)
    f = call("/api/files/" + src["file_id"])[1]["file"]
    check("引用处显示的是新名（按 file_id 查）",
          f["mentions"] and f["mentions"][0]["name"] == "zz-体检-目标改名.md", f["mentions"])
    check("边没断", len(f["mentions"]) == 1, len(f["mentions"]))
    # 新行为：改名会把引用处的正文一起改掉（不再留"正文里还是旧名"的烂摊子）
    body_now = call("/api/files/" + src["file_id"])[1]["file"]["content"]
    check("引用处的正文被自动改成了新名", "@zz-体检-目标改名" in body_now, body_now[:80])
    st, saved = call("/api/files/" + src["file_id"], "PUT", {"content": body_now})
    check("改写后重存不断链",
          saved.get("saved", {}).get("mentions") == 1 and not saved.get("saved", {}).get("unresolved"),
          saved.get("saved"))

    print("④ 移动文件到 B")
    st, mv = call("/api/files/%s/move" % src["file_id"], "POST", {"folder_id": b["folder_id"]})
    check("移动接口 200", st == 200 and mv["path"] == "zz-体检-B", mv)
    tree = call("/api/tree")[1]["tree"]


    def dir_named(n, name):
        if n.get("name") == name:
            return n
        for c in (n.get("children") or []):
            r = dir_named(c, name)
            if r:
                return r
        return None


    node_b = dir_named(tree, "zz-体检-B")
    check("树里它出现在 B 下", node_b and any(x["name"] == "zz-体检-引用方.md" for x in node_b["files"]),
          [x["name"] for x in (node_b or {}).get("files", [])])

    print("⑤ 文件夹改名 + 删空文件夹")
    st, r = call("/api/folders/%s/rename" % b["folder_id"], "POST", {"name": "zz-体检-B2"})
    check("文件夹改名 200", st == 200 and r["path"] == "zz-体检-B2", r)
    st, r = call("/api/folders/%s" % a["folder_id"], "DELETE")
    check("非空文件夹拒删（400）", st == 400, r.get("error"))
    st, c = call("/api/folders", "POST", {"name": "zz-体检-C"})
    made["folders"].append(c["folder_id"])
    st, r = call("/api/folders/%s" % c["folder_id"], "DELETE")
    check("空文件夹可删（200）", st == 200, r)
    made["folders"].remove(c["folder_id"])

    print("⑥ 事件齐不齐")
    evs = events_after(base)
    for t in ("folder.created", "folder.renamed", "folder.deleted", "note.created",
              "note.renamed", "note.moved", "note.updated"):
        check("有 %s" % t, bool(find(evs, t)), len(find(evs, t)))
    mv_ev = find(evs, "note.moved")
    check("note.moved 带了 from/to",
          mv_ev and mv_ev[0]["payload"].get("from_folder_id") and mv_ev[0]["payload"].get("to_folder_id"),
          mv_ev[0]["payload"] if mv_ev else None)
    rn_ev = find(evs, "note.renamed")
    check("note.renamed 带了 old/new",
          rn_ev and rn_ev[0]["payload"].get("old_name") == "zz-体检-目标.md"
          and rn_ev[0]["payload"].get("new_name") == "zz-体检-目标改名.md",
          rn_ev[0]["payload"] if rn_ev else None)
finally:
    print("⑦ 清场")
    for fid in made["files"]:
        call("/api/files/" + fid, "DELETE")
    # 先删 B2 里的文件再删空目录
    for f in call("/api/folders")[1]["folders"]:
        if f["name"].startswith("zz-体检-") and f["children"] == 0:
            call("/api/folders/" + f["id"], "DELETE")
    h = call("/api/health")[1]
    left = [f["name"] for f in call("/api/folders")[1]["folders"] if f["name"].startswith("zz-体检-")]
    check("临时文件夹清干净", not left, left)
    check("文件数复原", h["stats"]["files"] == files_before,
          "%s vs %s" % (h["stats"]["files"], files_before))
    check("没留下语义索引缺口", h["pending_vectors"] <= pend_before,
          "%s vs %s" % (h["pending_vectors"], pend_before))

print("\n========  结论：%s" % ("全部通过" if ok_all else "有失败项"))
sys.exit(0 if ok_all else 1)
