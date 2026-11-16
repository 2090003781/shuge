"""软件操作权限 + 执行通道的自测（平台侧）。

  python scripts/smoke_permissions.py

验：目录条数与三档分布；按档校验（read 档拒 write 操作、write 档拒 trust 操作）；
冒名插件被拒；带路径参数的操作能落到库；档位能存能读能回滚。
"""

import os as _os, sys as _sys          # 门禁用（2026-09-30 体检）

if "--yes" not in _sys.argv and _os.environ.get("COREHOST_ALLOW_DESTRUCTIVE") != "1":
    print(__doc__ or "")
    print("\n【门禁】%s 会真的动到**运行中的枢阁 / 库文件夹**（这个脚本忽略参数，--help 也照跑）。"
          % _os.path.basename(__file__))
    print("       确认要跑：python %s --yes（或设 COREHOST_ALLOW_DESTRUCTIVE=1）"
          % _os.path.basename(__file__))
    _sys.exit(2)
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("CORE_URL", "http://127.0.0.1:8790")
PLUGIN = "plugin.dsh"
OK = True


def call(path, body=None, method="GET"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:200]}


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:140] if extra else ""))


PERM_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "plugins", "permissions.json")
orig = None
try:                                  # 跑之前按字节留底：这个自测会真改档位
    with open(PERM_FILE, "rb") as _f:
        _ORIG_PERMS = _f.read()
except OSError:
    _ORIG_PERMS = None
try:
    print("① 目录与档位")
    st, d = call("/api/permissions?plugin=" + PLUGIN)
    check("接口在", st == 200 and d.get("ok") is True, st)
    check("三档齐全", [l["id"] for l in d.get("levels", [])] == ["read", "write", "trust"],
          d.get("levels"))
    # 38 条 = 只读 17 + 可写 9 + 相信 12
    #   2026-09-21 加：rag_list(read) / import(trust) / rag_batch(trust)
    #   2026-09-22 加：table_list(read) / table_rows(read) / table_import(trust) / table_drop(write)
    lv = {}
    for o in d.get("catalog", []):
        lv[o["level"]] = lv.get(o["level"], 0) + 1
    check("目录 38 条", d.get("total") == 38 and len(d.get("catalog", [])) == 38, d.get("total"))
    check("分档 17/9/12", (lv.get("read"), lv.get("write"), lv.get("trust")) == (17, 9, 12), lv)
    check("每档都有操作", all(any(o["level"] == lv for o in d["catalog"])
                             for lv in ("read", "write", "trust")))
    orig = d.get("level")
    print("     当前档位：%s（释放 %s/%s）" % (orig, d.get("released"), d.get("total")))

    print("② 执行通道：只读操作")
    st, d = call("/api/ops/call", {"plugin": PLUGIN, "op": "brain.health"}, "POST")
    check("brain.health 跑通", st == 200 and d.get("ok") is True,
          "库文件数 %s" % ((d.get("result") or {}).get("stats", {}) or {}).get("files"))

    print("③ 带路径参数的操作")
    st, d = call("/api/ops/call", {"plugin": PLUGIN, "op": "brain.search",
                                   "args": {"q": "契约", "limit": 1}}, "POST")
    fid = (((d.get("result") or {}).get("hits") or [{}])[0] or {}).get("file_id")
    if fid:
        st, d2 = call("/api/ops/call", {"plugin": PLUGIN, "op": "brain.note",
                                        "args": {"id": fid}}, "POST")
        check("brain.note 填了 {id} 也能跑", d2.get("ok") is True,
              ((d2.get("result") or {}).get("file") or {}).get("name"))
    else:
        check("brain.note 填了 {id} 也能跑", False, "检索没命中，跳过")

    print("④ 按档校验")
    call("/api/permissions/set", {"plugin": PLUGIN, "level": "read"}, "POST")
    st, d = call("/api/ops/call", {"plugin": PLUGIN, "op": "brain.create",
                                   "args": {"name": "zz-档位自测.md", "content": "# x"}}, "POST")
    check("read 档拒 write 操作", d.get("denied") is True and d.get("need") == "write",
          d.get("error"))
    st, d = call("/api/ops/call", {"plugin": PLUGIN, "op": "brain.search",
                                   "args": {"q": "契约"}}, "POST")
    check("read 档放只读操作", d.get("ok") is True)
    call("/api/permissions/set", {"plugin": PLUGIN, "level": "write"}, "POST")
    st, d = call("/api/ops/call", {"plugin": PLUGIN, "op": "brain.delete",
                                   "args": {"id": "whatever"}}, "POST")
    check("write 档拒 trust 操作", d.get("denied") is True and d.get("need") == "trust",
          d.get("error"))
    call("/api/permissions/set", {"plugin": PLUGIN, "level": "trust"}, "POST")
    st, d = call("/api/ops/call", {"plugin": PLUGIN, "op": "brain.delete",
                                   "args": {"id": "zz-不存在-不该删掉东西"}}, "POST")
    check("trust 档放行（库返回'没这个文件'也算放行）",
          d.get("denied") is not True and d.get("op_level") == "trust", d.get("status"))

    print("⑤ 冒名插件")
    st, d = call("/api/ops/call", {"plugin": "plugin.fake", "op": "brain.health"}, "POST")
    check("没登记的插件被拒", d.get("ok") is False and "不是已登记" in (d.get("error") or ""),
          d.get("error"))

    print("⑥ 落盘")
    pf = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "data", "plugins", "permissions.json")
    saved = json.load(open(pf, encoding="utf-8")) if os.path.isfile(pf) else {}
    check("档位写进了 data/plugins/permissions.json", saved.get(PLUGIN) == "trust", saved)
finally:
    print("⑦ 还原档位")
    if orig in ("read", "write", "trust"):
        call("/api/permissions/set", {"plugin": PLUGIN, "level": orig}, "POST")
        print("     已还原为 %s" % orig)
    if _ORIG_PERMS is not None:        # 整份文件按字节还回去（不只还档位）
        try:
            with open(PERM_FILE + ".restore-tmp", "wb") as _f:
                _f.write(_ORIG_PERMS)
            os.replace(PERM_FILE + ".restore-tmp", PERM_FILE)
            print("     权限文件已按字节还原")
        except OSError as _e:
            print("     ⚠️ 权限文件还原失败：%s" % _e)

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
