# -*- coding: utf-8 -*-
"""H 健康接口不许吐密钥材料（2026-09-30 体检）。

  python tools/smoke_health_secret.py [BASE]

背景：`/api/health` 的 manifest 里原来带 `kdf_salt` / `kdf_n·r·p` / `verify_blob` ——
这三样凑齐就能**离线猜库密码**（scrypt N=32768 只是拖慢，不改变可行性）。
口径与 `export.py` 一致：绝不外发密钥材料。

这个检查要对着**真库**跑才有意义（真库才设过密码）；空库上它只是不报错。
"""
import json
import sys
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
BAD_KEYS = ("kdf_algo", "kdf_salt", "kdf_n", "kdf_r", "kdf_p", "verify_blob")

with urllib.request.urlopen(BASE + "/api/health", timeout=60) as r:
    d = json.loads(r.read().decode("utf-8"))

man = d.get("manifest") or {}
leak = [k for k in BAD_KEYS if k in man]
for k in leak:
    print("❌ manifest 里有 %s = %r" % (k, str(man.get(k))[:40]))
st = urllib.request.urlopen(BASE + "/api/lock", timeout=30)
lock = json.loads(st.read().decode("utf-8"))
print("[i] /api/health manifest 键：%d 个；/api/lock.configured = %s（锁状态该走这个接口）"
      % (len(man), lock.get("configured")))
print("通过 1 / 1；失败：无" if not leak else "失败：%d 个密钥字段在泄漏" % len(leak))
sys.exit(1 if leak else 0)
