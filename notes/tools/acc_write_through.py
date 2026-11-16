# -*- coding: utf-8 -*-
""""库改了，磁盘必须跟着改"的验收（2026-09-27 审计 A/B/C/E 的常驻断言）。

铁律：**库文件夹就是库的投影** —— 不看库内那一份，只看磁盘那份对不对：
  · 改名带出来的"引用方正文改写"要落盘；
  · 文件夹改名，库文件夹里那棵目录名也要跟着改（旧目录不许留）；
  · 另存为 / 导出的产物要落盘；
  · 删文件夹要把库文件夹里那个目录（连同库里不认识的游离文件）收掉。

只走接口，测完清场。用法：python tools/acc_write_through.py
"""
import json
import os
import time
import urllib.error
import urllib.request

# 支持指向别的服务（2026-10-15）：临时服务可以加载**新代码**，
# 而常驻库里跑的是它启动时的代码 —— 验收新改动必须能换地址。
import sys as _sys
BASE = (_sys.argv[1] if len(_sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
PASS = 0
FAIL = []


def call(path, method="GET", body=None, timeout=300):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    last = None
    for _ in range(20):
        req = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={"Content-Type": "application/json",
                                              "X-Writer": "tool:acc-writethrough"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception as e:
            last = e
            time.sleep(3)
    raise last


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
    print("  %s %s%s" % ("✅" if cond else "❌", name, "" if cond else "  | " + str(extra)[:220]))
    if not cond:
        FAIL.append(name)


def read(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def main():
    _, h = call("/api/health")
    LIB = (h.get("manifest") or {}).get("library_dir") or ""
    if not LIB or not os.path.isdir(LIB):
        print("❌ 没配库文件夹，跳过")
        return 1
    root = os.path.join(LIB, "zz-写盘-甲")
    sub = os.path.join(root, "子目录")
    sub2 = os.path.join(root, "子目录2")

    def cleanup():
        _, tree = call("/api/tree")
        found = []

        def walk(n):
            for f in n.get("files") or []:
                found.append((f["id"], f["name"]))
            for c in n.get("children") or []:
                walk(c)
        walk(tree["tree"])
        for fid, nm in found:
            if nm.startswith("zz-"):
                call("/api/files/" + fid, "DELETE")
        _, fs = call("/api/folders")
        # 按路径删（子目录名不含 zz-，只看名字会漏），深到浅
        for f in sorted(fs.get("folders") or [], key=lambda x: -len(x.get("path") or "")):
            if "zz-写盘" in (f.get("path") or ""):
                call("/api/folders/" + f["id"], "DELETE")

    cleanup()
    print("① 建文件夹 / 建篇：接口写完就该落在库文件夹里")
    st, fa = call("/api/folders", "POST", {"path": "vault", "name": "zz-写盘-甲"})
    st2, fb = call("/api/folders", "POST", {"parent_id": fa["folder_id"], "name": "子目录"})
    st3, tgt = call("/api/files", "POST", {"folder_id": fb["folder_id"], "name": "zz-目标.md",
                                           "content": "# 目标\n\n目标正文。\n"})
    st4, src = call("/api/files", "POST", {"folder_id": fb["folder_id"], "name": "zz-引用方.md",
                                           "content": "# 引用方\n\n看 @zz-目标 这一篇。\n"})
    check("三件都建好了", st in (200, 201) and st2 in (200, 201)
          and st3 in (200, 201) and st4 in (200, 201), [st, st2, st3, st4])
    check("目标篇落在库文件夹里", os.path.isfile(os.path.join(sub, "zz-目标.md")),
          os.listdir(root) if os.path.isdir(root) else "目录不存在")
    check("引用方落在库文件夹里", os.path.isfile(os.path.join(sub, "zz-引用方.md")), None)

    print("② 改名：被改写的「引用方正文」必须也落盘（审计 A）")
    call("/api/files/%s/rename" % tgt["file_id"], "POST", {"name": "zz-目标改名.md"})
    disk = read(os.path.join(sub, "zz-引用方.md")) or ""
    check("库文件夹里那份引用方已经改成新名", "@zz-目标改名" in disk, disk.replace("\n", "⏎")[:120])
    check("旧名不再出现在磁盘那份里", "@zz-目标 " not in disk, disk.replace("\n", "⏎")[:120])
    check("改名后新名文件在、旧名文件没了",
          os.path.isfile(os.path.join(sub, "zz-目标改名.md")) and not os.path.isfile(os.path.join(sub, "zz-目标.md")),
          os.listdir(sub))

    print("③ 文件夹改名：库文件夹里那棵目录名也要跟着改（审计 A）")
    call("/api/folders/%s/rename" % fb["folder_id"], "POST", {"name": "子目录2"})
    check("新目录名出现", os.path.isdir(sub2), os.listdir(root))
    check("旧目录名不在了", not os.path.isdir(sub), os.listdir(root))
    check("里面的篇跟着挪过去了", os.path.isfile(os.path.join(sub2, "zz-引用方.md")), None)

    print("④ 移动：新位置有、旧位置没有")
    call("/api/files/%s/move" % src["file_id"], "POST", {"folder_id": fa["folder_id"]})
    check("新位置有这篇", os.path.isfile(os.path.join(root, "zz-引用方.md")), os.listdir(root))
    check("旧位置没有了", not os.path.isfile(os.path.join(sub2, "zz-引用方.md")), os.listdir(sub2))

    print("⑤ 另存为 Markdown：产物也要落盘（审计 C）")
    st5, cv = call("/api/files/%s/convert" % tgt["file_id"], "POST", {"to": "md"})
    prod = (cv or {}).get("name") or ""
    check("转换接口 200 且有产物名", st5 in (200, 201) and bool(prod), cv)
    check("产物在库文件夹里", bool(prod) and os.path.isfile(os.path.join(sub2, prod)),
          os.listdir(sub2))

    print("⑥ 导入一篇带 @引用的：库里要真的有这条边（审计 D）")
    import tempfile
    with tempfile.TemporaryDirectory(prefix="acc-import-") as td:
        p = os.path.join(td, "zz-导入-引用方.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("# 导入的引用方\n\n看 @zz-目标改名 这一篇。\n")
        st6, imp = call("/api/import", "POST", {"src": p, "under": "vault", "overwrite": True})
    check("导入接口 200", st6 in (200, 201), imp)
    _, fs2 = call("/api/tree")
    found = []

    def walk2(n):
        for f in n.get("files") or []:
            found.append(f)
        for c in n.get("children") or []:
            walk2(c)

    walk2(fs2["tree"])
    imp_id = next((f["id"] for f in found if f["name"] == "zz-导入-引用方.md"), "")
    check("导入的篇在库里", bool(imp_id), [f["name"] for f in found][-4:])
    _, one = call("/api/files/" + imp_id) if imp_id else (0, {})
    check("导入的篇建出了 @提及边", bool((one.get("file") or {}).get("mentions")), (one.get("file") or {}).get("mentions"))

    print("⑦ 前像恢复：磁盘那份也要跟着变回来（审计 K）")
    _, snap = call("/api/before-images?file_id=" + tgt["file_id"])
    imgs = snap.get("images") or snap.get("before_images") or []
    if not imgs:
        call("/api/files/" + tgt["file_id"], "PUT", {"content": "# 覆盖一版\n\n换了内容。\n", "allow_shrink": True})
        _, snap = call("/api/before-images?file_id=" + tgt["file_id"])
        imgs = snap.get("images") or snap.get("before_images") or []
    if not imgs:
        check("拿得到前像", False, snap)
    else:
        st7, rr = call("/api/before-images/restore", "POST", {"id": imgs[0].get("id")})
        check("恢复接口 200", st7 == 200 and rr.get("ok") is not False, rr)
        disk2 = read(os.path.join(sub2, "zz-目标改名.md")) or ""
        check("磁盘那份已经跟着恢复（不是旧的覆盖版）", "换了内容" not in disk2, disk2.replace("\n", "⏎")[:120])

    print("⑧b 内容没变、但盘上那份被弄坏 → 再导一次要把投影补回来（2026-10-04 静默盲区）")
    import tempfile as _tf
    _disk = os.path.join(LIB, "zz-补投影.md")
    _body = "# 补投影\n\n正文一二三。\n"
    _keep_id = ""
    with _tf.TemporaryDirectory(prefix="acc-proj-") as td:
        _p = os.path.join(td, "zz-补投影.md")
        with open(_p, "w", encoding="utf-8") as f:
            f.write(_body)
        st_a, imp_a = call("/api/import", "POST", {"src": _p, "under": "vault", "overwrite": True})
        _keep_id = (imp_a.get("created_ids") or [""])[0]
        check("先导一次：盘上有、内容对", st_a in (200, 201) and read(_disk) == _body,
              (st_a, read(_disk)[:40]))
        with open(_disk, "w", encoding="utf-8") as f:      # 弄坏它（模拟"当时写入被占用"）
            f.write("坏的")
        st_b, imp_b = call("/api/import", "POST", {"src": _p, "under": "vault", "overwrite": True})
    check("内容没变（skip）也要把盘上那份补回来",
          imp_b.get("skipped") == 1 and (imp_b.get("tree") or {}).get("ok", 0) >= 1
          and read(_disk) == _body,
          {"skipped": imp_b.get("skipped"), "tree": imp_b.get("tree"), "盘上": read(_disk)[:40]})
    if _keep_id:
        call("/api/files/" + _keep_id, "DELETE")           # 收摊：别把测试篇留在库里

    print("⑧ 镜像管线已退役（2026-09-30 归档）：同步入口回 410，库内写入照旧只落库文件夹")
    _st_ret, ret = call("/api/mirror/sync", "POST", {})
    check("同步入口已退役（410 + retired 标记）",
          _st_ret == 410 and ret.get("retired") is True, (_st_ret, {k: ret.get(k) for k in ("retired", "error")}))
    check("说明里给了取回路径", "archive/2026-09-30-废弃能力" in str(ret.get("hint") or ""), ret.get("hint"))
    call("/api/files", "POST", {"folder_id": fa["folder_id"], "name": "zz-开关-仍落盘.md",
                                "content": "# 开关\n\n镜像退役了也要落库文件夹。\n"})
    check("库内新建照样落到库文件夹", os.path.isfile(os.path.join(root, "zz-开关-仍落盘.md")),
          os.listdir(root))

    print("⑨ 清场：删篇 + 删文件夹 → 库文件夹里那个目录也要收掉")
    cleanup()
    time.sleep(0.4)
    check("测试目录已经从库文件夹消失", not os.path.isdir(root),
          os.listdir(LIB)[:12] if os.path.isdir(LIB) else LIB)

    print("\n========  结论: %d 通过 / %d 失败" % (PASS, len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
