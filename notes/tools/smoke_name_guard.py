# -*- coding: utf-8 -*-
"""名字逃逸冒烟：库文件夹路径不能被「名字」甩出去（2026-09-30 体检 H1/H2 的回归测试）。

  python tools/smoke_name_guard.py

为什么要它：`tree_write` 把「文件夹路径 + 名字」直接拼成磁盘路径，所以名字里出现 `/`、`\\`、`..`
或盘符时，接口就能写到库文件夹**外面**（实测能覆盖 D 盘任意文件），文件夹名叫 `..` 再 DELETE
会递归删掉**父目录**。修法是两道：入口 `db.check_name`、落盘 `tree_write._inside/safe_rel`。
这个脚本把两道都盯住 —— 自己起一个**临时库**（真库完全不碰），库文件夹指向临时目录里的沙箱。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

OK, BAD = [], []


def case(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print("  %s %s%s" % ("✅" if cond else "❌", name, ("  " + extra) if extra else ""))
    return cond


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_health(base, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def req(base, path, body=None, method=None, timeout=120):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = urllib.request.Request(base + path, data=data,
                               headers={"Content-Type": "application/json",
                                        "X-Writer": "tool:smoke-name-guard"}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return "ERR", {"error": "%s: %s" % (type(e).__name__, str(e)[:120])}


def main():
    # ── ① 纯函数：safe_rel / _inside ──────────────────────
    print("① safe_rel（把库内相对路径规整；有退路就拒）")
    import tree_write as TW
    from pathlib import Path
    for src, want in [("a/b.md", "a/b.md"), ("vault/a/b.md", "vault/a/b.md"),
                      ("a\\b.md", "a/b.md"), ("./a//b.md", "a/b.md"),
                      ("../x", ""), ("..", ""), ("a/../../x", ""), ("/abs/x", ""),
                      ("C:/x", ""), ("c:\\x", ""), ("vault/../../../x", "")]:
        got = TW.safe_rel(src)
        case("safe_rel(%-16r) = %r" % (src, got), got == want, "want %r" % want)

    print("② _inside（落点必须真在库文件夹里）")
    root = Path(r"C:\tmp\lib")
    case("_inside(root, root\\a.md)", TW._inside(root, root / "a.md") is True)
    case("_inside(root, root\\..\\a.md) = False", TW._inside(root, root / ".." / "a.md") is False)
    case("_inside(root, C:\\other\\a.md) = False", TW._inside(root, Path(r"C:\other\a.md")) is False)

    # ── ③ 起临时库，打真实请求 ────────────────────────────
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    data = tempfile.mkdtemp(prefix="ai-notes-nameguard-")
    sandbox = tempfile.mkdtemp(prefix="zz-nameguard-")
    lib = os.path.join(sandbox, "lib")
    os.makedirs(lib, exist_ok=True)
    open(os.path.join(sandbox, "CANARY.txt"), "w", encoding="utf-8").write("父目录哨兵\n")
    srv = None
    try:
        srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "src", "server.py"),
                                "--port", str(port), "--data", data],
                               cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=NO_WINDOW)
        if not wait_health(base):
            print("临时库没起来（port=%d）" % port)
            return 1
        print("③ 临时库就绪 %s（库文件夹=%s）" % (base, lib))
        st, d = req(base, "/api/mirror/config", {"library_dir": lib,
                                                 "vault_dir": os.path.join(sandbox, "vault")})
        case("把 library_dir 指到沙箱", st == 200 and (d.get("library_dir") or "").endswith("lib"))

        print("④ 入口校验：这些名字必须 400")
        bad_names = ["../ESCAPED.txt", "..", ".", "a/b.md", "a\\b.md", "/abs.md",
                     os.path.join(sandbox, "ABS.txt"), "x:y.md", "trail."]
        for nm in bad_names:
            st, d = req(base, "/api/files", {"name": nm})
            case("建文件 name=%-28r → 400" % nm, st == 400, "got %s %s" % (st, (d.get("error") or "")[:40]))

        # 空名字不是逃逸：接口本来就把它兜成 untitled.md（2026-09-30 前就有）。只要求落点是安全默认名。
        st, d = req(base, "/api/files", {"name": ""})
        _nm = ((d.get("saved") or {}).get("name") if isinstance(d, dict) else "") or "untitled.md"
        case("空名字兜成安全默认名", st in (200, 201) and "/" not in _nm and "\\" not in _nm, "got %s %r" % (st, _nm))

        print("⑤ 入口校验：文件夹名同理")
        for nm in ["..", "../..", "a/b", "..\\x"]:
            st, d = req(base, "/api/folders", {"name": nm})
            case("建文件夹 name=%-12r → 400" % nm, st == 400, "got %s" % st)

        print("⑥ 正常名字照旧能用（别把好路堵了）")
        st, d = req(base, "/api/files", {"name": "zz-t-笔记.md"})
        fid = (d or {}).get("file_id")
        case("建 zz-t-笔记.md → 201", st == 201 and bool(fid), "got %s" % st)
        st, d = req(base, "/api/folders", {"name": "zz-t-目录"})
        dfid = (d or {}).get("folder_id")
        case("建文件夹 zz-t-目录 → 201", st == 201 and bool(dfid), "got %s" % st)
        if dfid:
            st, d = req(base, "/api/files", {"folder_id": dfid, "name": "子笔记.md"})
            case("在子文件夹里建 子笔记.md → 201", st == 201, "got %s" % st)

        print("⑦ 改名也不能被甩出去")
        for nm in ["../ESCAPED.txt", "..", os.path.join(sandbox, "ABS.txt")]:
            st, d = req(base, "/api/files/%s/rename" % fid, {"name": nm})
            case("rename name=%-30r → 400" % nm[:30], st == 400, "got %s" % st)
        st, d = req(base, "/api/files/%s/rename" % fid, {"name": "zz-t-改名.md"})
        case("rename 成 zz-t-改名.md → 200", st == 200, "got %s" % st)

        print("⑧ 落盘兜底：老数据里就有坏名字时也得拦住")
        # 直接往临时库里塞一行「名字带 ..」的老数据，再让 tree_write 去写它
        import db as DBmod
        d2 = DBmod.Database(data)
        root_id = d2.root_folder_id()
        bad_id = d2.create_file(root_id, "good.md", b"x")          # 先正常建（走校验）
        d2.conn.execute("UPDATE files SET name=? WHERE id=?", ("../LEGACY-ESCAPE.md", bad_id))
        d2.conn.commit()
        res = TW.write_through(d2, bad_id)
        case("write_through 拒绝 ../LEGACY-ESCAPE.md（rel_of 先把它规整成空）",
             res.get("ok") is False, str(res)[:60])
        res = TW.drop_dir(d2, "..")
        case("drop_dir('..') 说不", res.get("ok") is False, str(res)[:60])
        res = TW.drop_dir(d2, "../..")
        case("drop_dir('../..') 说不", res.get("ok") is False, str(res)[:60])
        d2.close()

        print("⑨ 沙箱外面一个多余文件都不许有")
        stray = [x for x in os.listdir(sandbox) if x not in ("lib", "CANARY.txt", "vault")]
        case("沙箱根目录没有逃逸产物", not stray, str(stray))
        case("父目录哨兵 CANARY.txt 还在", os.path.isfile(os.path.join(sandbox, "CANARY.txt")))
        inside = sorted(os.listdir(lib))
        case("库文件夹里只有该有的东西", all(not x.upper().startswith(("ESCAPED", "ABS")) for x in inside),
             str(inside))
    finally:
        if srv:
            srv.terminate()
            try:
                srv.wait(timeout=10)
            except Exception:
                try:
                    srv.kill()
                except Exception:
                    pass
        shutil.rmtree(sandbox, ignore_errors=True)
        shutil.rmtree(data, ignore_errors=True)

    print()
    print("通过 %d / %d；失败：%s" % (len(OK), len(OK) + len(BAD), ("、".join(BAD) if BAD else "无")))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
