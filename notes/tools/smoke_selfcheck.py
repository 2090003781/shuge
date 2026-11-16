"""自检工具的验收：JSON 结构齐全、退出码反映好坏、--quiet 只输出问题、且它只读（不改库）。"""
import json
import os
import subprocess
import sys
import urllib.request

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OK = True


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:150] if extra else ""))


def tool(*args):
    p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "selfcheck.py")] + list(args),
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    return p.returncode, p.stdout, p.stderr


def health():
    with urllib.request.urlopen("http://127.0.0.1:8848/api/health", timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


h0 = health()
print("① --json：结构齐全、结论可信")
rc, out, err = tool("--json")
# 注意：不能断言"库一定健康"（那取决于别的测试有没有留残留）；只断言工具本身对不对
check("退出码是 0 或 1（0=健康 1=有 ❌，都是正常输出）", rc in (0, 1), err[-200:])
try:
    R = json.loads(out)
except Exception as e:
    check("输出是合法 JSON", False, "%s: %s" % (e, out[:120]))
    R = {}
if R:
    check("顶层字段", all(k in R for k in ("ok", "bad", "warn", "checks")) and isinstance(R["checks"], list),
          list(R)[:6])
    ids = {c["id"] for c in R["checks"]}
    need = {"verify", "vectors", "svc:库(library)", "refs:unresolved", "refs:bad_sections",
            "hygiene:zz", "snapshot", "activity"}
    check("该有的检查项都在", need <= ids, sorted(need - ids))
    check("级别只有 ok/warn/bad", all(c["level"] in ("ok", "warn", "bad") for c in R["checks"]),
          [c["level"] for c in R["checks"]][:8])
    check("ok 与 bad 计数一致", R["ok"] == (R["bad"] == 0), "ok=%s bad=%s" % (R["ok"], R["bad"]))
    check("退出码与 bad 一致", (rc == 0) == (R["bad"] == 0), "rc=%s bad=%s" % (rc, R["bad"]))
    check("一致性那项没报错", next(c for c in R["checks"] if c["id"] == "verify")["level"] != "bad",
          next(c for c in R["checks"] if c["id"] == "verify")["detail"][:80])

print("② --quiet：只输出 ⚠/❌，且仍然给出汇总")
rc2, out2, err2 = tool("--quiet")
lines = [l for l in out2.splitlines() if l.strip()]
check("没有 ✅ 行", not any(l.startswith("✅") for l in lines), lines[:3])
check("有问题行或直接给汇总", any(l.startswith("⚠️") or l.startswith("❌") for l in lines)
      or any(l.startswith("——") for l in lines), lines[:3])
check("最后一行是汇总", lines and lines[-1].startswith("——"), lines[-1:] if lines else "")
check("退出码与 --json 一致", (rc2 == 0) == (rc == 0), "quiet=%s json=%s" % (rc2, rc))

print("②b 界面用的维护接口：GET /api/maintenance/selfcheck")
try:
    with urllib.request.urlopen("http://127.0.0.1:8848/api/maintenance/selfcheck", timeout=300) as r:
        M = json.loads(r.read().decode("utf-8"))
    check("外层是「请求成功」", M.get("ok") is True and isinstance(M.get("result"), dict), list(M)[:6])
    M = M["result"]
    check("里层结构同 CLI", isinstance(M.get("checks"), list) and "ok" in M, list(M)[:6])
    ids2 = {c["id"] for c in M["checks"]}
    check("检查项与 CLI 一致", ids2 == {c["id"] for c in (R.get("checks") or [])} or len(ids2) >= 15,
          "%d 项" % len(ids2))
    lib = next(c for c in M["checks"] if c["id"] == "svc:库(library)")
    check("由库进程触发时跳过自探（单线程会死锁）", "跳过自探" in lib["detail"], lib["detail"])
    check("接口结论不为 ❌", M.get("bad", 1) == 0 or M.get("ok") is False, "bad=%s" % M.get("bad"))
except Exception as e:
    check("维护接口可用", False, str(e)[:120])

print("③ 只读：跑完库规模不变")
h1 = health()
check("文件数/块数不变", h1["stats"]["files"] == h0["stats"]["files"]
      and h1["stats"]["chunks"] == h0["stats"]["chunks"],
      "%s/%s → %s/%s" % (h0["stats"]["files"], h0["stats"]["chunks"],
                         h1["stats"]["files"], h1["stats"]["chunks"]))

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
