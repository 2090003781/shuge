# -*- coding: utf-8 -*-
"""回收站的验收（辞玖 2026-09-28：「库的回收站加回来」）。

界面那一页（🗑 回收站）只是两层接口的皮，所以这里验接口本身：

  A 部分（对着真库，不动锁状态）
    ① 删除 → 必须留下前像（不然「回收」这词就是空的）
    ② 清单里看得见它：名字 / 原因 / 时间 / 字节 / 落盘路径都得有
    ③ 按前像恢复 → 那篇真的回来，而且**逐字是原文**
    ④ 清理旧前像：锁着必须 403（前像是明文落盘的，不许在看不见的时候悄悄删）

  B 部分（对着临时库，永远跑得到「能清」那一侧）
    ⑤ days=0 被拒；days=1 能跑，且不该清的不会被清
    ⑥ 真删：把一条前像改成 3 天前，days=1 必须把**文件**也从磁盘上收走
       —— 为什么单独起临时库：真库锁不锁由辞玖决定，不该为了测试去动它。
"""
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
# 可选位置参数：指向别的服务（临时服务可加载**新代码**，验收改动必须能换地址）
BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
PASS = 0
FAIL = []
NAME = "zz-回收站-验证.md"
BODY = "# 回收站验证\n\n这句话是给「恢复后逐字比对」用的：辞玖 9527。\n"


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
    print("  %s %s%s" % ("✅" if cond else "❌", name, "" if cond else "  | " + str(extra)[:200]))
    if not cond:
        FAIL.append(name)


def call(path, method="GET", body=None, base=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request((base or BASE) + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "X-Writer": "tool:bin-acc"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


# ══ A：真库（按当前锁状态断言） ═════════════════════════════
files_before = call("/api/health")[1]["stats"]["files"]
lock = call("/api/lock")[1]
locked = not lock.get("unlocked")
print("A. 真库：%d 篇 · 锁：configured=%s unlocked=%s"
      % (files_before, lock.get("configured"), locked))

fid = ""
new_fid = ""
try:
    print("① 删除一篇 → 必须留下前像")
    st, made = call("/api/files", "POST", {"name": NAME, "content": BODY})
    fid = made["file_id"]
    check("临时笔记已建", st == 201 and bool(fid), made)
    st, d0 = call("/api/files/" + fid)
    check("建好时内容就是原文", d0["file"]["content"] == BODY, len(d0["file"]["content"]))
    st, dl = call("/api/files/" + fid, "DELETE")
    check("删除成功", st in (200, 404), dl)

    print("② 清单里看得见它")
    st, bi = call("/api/before-images?file_id=%s" % fid)
    imgs = bi.get("images") or []
    check("该篇的前像列出来了", st == 200 and len(imgs) >= 1, len(imgs))
    rec = imgs[0] if imgs else {}
    check("原因标成删除", rec.get("reason") == "delete", rec.get("reason"))
    check("名字/路径/时间/字节都在",
          rec.get("name") == NAME and bool(rec.get("path")) and bool(rec.get("at"))
          and isinstance(rec.get("bytes"), int) and rec.get("bytes") == len(BODY.encode("utf-8")),
          {k: rec.get(k) for k in ("name", "path", "at", "bytes")})
    check("前像落在 前像/ 目录里（有 image 字段）",
          str(rec.get("image") or "").startswith("前像/"), rec.get("image"))
    check("写入者标成 tool:bin-acc（不是没人认领的 auto:）",
          rec.get("writer") == "tool:bin-acc", rec.get("writer"))

    print("③ 按前像恢复 → 那篇回来，而且逐字是原文")
    st, rr = call("/api/before-images/restore", "POST", {"id": rec.get("id")})
    check("恢复成功", st == 200 and rr.get("ok"), rr)
    check("原篇已被删 → 走的是「重建」那条路", rr.get("mode") == "recreated", rr.get("mode"))
    new_fid = rr.get("file_id") or ""
    check("重建出一个新 id", bool(new_fid) and new_fid != fid, (new_fid[:8], fid[:8]))
    st, back = call("/api/files/" + new_fid)
    check("恢复后正文逐字一致", back["file"]["content"] == BODY,
          repr(back["file"]["content"][:40]))
    check("名字也对", back["file"]["name"] == NAME, back["file"]["name"])
    st, bi2 = call("/api/before-images?file_id=%s" % new_fid)
    check("新 id 名下没有前像（恢复不是「又删一次」）", not (bi2.get("images") or []),
          len(bi2.get("images") or []))

    print("④ 锁着的时候不许清（前像是明文落盘的）")
    st, pr = call("/api/before-images/prune", "POST", {"days": 1})
    if locked:
        check("锁着 → 403 + locked:true", st == 403 and pr.get("locked") is True, (st, pr))
    else:
        check("没锁 → 能跑", st == 200, (st, pr))
finally:
    print("⑤ 清场（真库）")
    for x in (new_fid, fid):
        if x:
            call("/api/files/" + x, "DELETE")
    st, hh = call("/api/health")
    check("库规模复原", hh["stats"]["files"] == files_before,
          "%s vs %s" % (hh["stats"]["files"], files_before))


# ══ B：临时库 —— 「能清」那一侧永远跑得到 ═══════════════════
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


print("\nB. 临时库（真库锁不锁不该影响这一侧的验收）")
port = free_port()
data = tempfile.mkdtemp(prefix="ai-notes-bin-")
tb = "http://127.0.0.1:%d" % port
srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "src", "server.py"),
                        "--port", str(port), "--data", data],
                       cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=NO_WINDOW)
try:
    if not wait_health(tb):
        check("临时库起来了", False, port)
    else:
        print("⑤ 入参闸 + 不清不该清的")
        st, made = call("/api/files", "POST", {"name": "zz-前像-清理.md", "content": "第一版\n"},
                        base=tb)
        tfid = made["file_id"]
        st, up = call("/api/files/" + tfid, "PUT",
                      {"content": "第二版（覆盖一次，留个前像）\n"}, base=tb)
        check("改一次正文（覆盖 → 留前像）", st == 200, up)
        st, bi3 = call("/api/before-images?file_id=%s" % tfid, base=tb)
        check("覆盖也留了前像", len(bi3.get("images") or []) >= 1, len(bi3.get("images") or []))
        st, pr = call("/api/before-images/prune", "POST", {"days": 0}, base=tb)
        check("days=0 被拒（400）", st == 400 and bool(pr.get("error")), (st, pr))
        st, pr = call("/api/before-images/prune", "POST", {"days": 1}, base=tb)
        check("days=1 能跑，返回 removed/kept",
              st == 200 and isinstance(pr.get("removed"), int)
              and isinstance(pr.get("kept"), int), pr)
        check("刚造的前像不会被清（removed=0）",
              pr.get("removed") == 0 and pr.get("kept", 0) >= 1, pr)

        print("⑥ 真删：把它改成 3 天前，days=1 必须连文件一起收走")
        idxp = os.path.join(data, "前像", "index.jsonl")
        lines = [json.loads(x) for x in open(idxp, encoding="utf-8") if x.strip()]
        old_at = lines[0]["at"]
        lines[0]["at"] = time.strftime("%Y-%m-%dT%H:%M:%S",
                                       time.localtime(time.time() - 3 * 86400))
        with open(idxp, "w", encoding="utf-8") as f:
            for r in lines:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        imgp = os.path.join(data, lines[0]["image"].replace("/", os.sep))
        check("改前：前像文件在磁盘上", os.path.isfile(imgp), imgp)
        st, pr = call("/api/before-images/prune", "POST", {"days": 1}, base=tb)
        check("days=1 清掉 1 条", st == 200 and pr.get("removed") == 1 and pr.get("kept") == 0,
              {"old_at": old_at, "resp": pr})
        check("前像文件真的从磁盘没了", not os.path.isfile(imgp), imgp)
        st, bi4 = call("/api/before-images?file_id=%s" % tfid, base=tb)
        check("清单里也没了", not (bi4.get("images") or []), bi4.get("images"))
finally:
    srv.terminate()
    try:
        srv.wait(timeout=10)
    except Exception:
        srv.kill()
    shutil.rmtree(data, ignore_errors=True)
    print("   （临时库与临时目录已清理，真库未被触碰）")

print("\n========  结论: %d 通过 / %d 失败" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
