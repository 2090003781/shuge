import base64, hashlib, json, os, shutil, sys
sys.stdout.reconfigure(encoding="utf-8")
BACKUP = r"D:\core-notes\data\tmp\b6_full\content_backup.jsonl"
rel = "日志/agent.dsh-raw/2026-10-14/会话-sched.md"
dst = os.path.join(r"D:\枢阁\库", *rel.split("/"))

rec = None
for line in open(BACKUP, encoding="utf-8"):
    r = json.loads(line)
    if r["rel"] == rel:
        rec = r; break
assert rec, "证据里没找到这一篇"
raw = base64.b64decode(rec["b64"])
assert hashlib.sha256(raw).hexdigest() == rec["sha"], "证据哈希对不上"

print("盘上现状 : %d 字节" % os.path.getsize(dst))
print("库内(真相): %d 字节  sha=%s" % (len(raw), rec["sha"][:16]))

# 前像留档（出事能退）
os.makedirs(r"D:\core-notes\data\tmp\b6_full\preimage", exist_ok=True)
pre = r"D:\core-notes\data\tmp\b6_full\preimage\会话-sched.md.2026-10-14.before"
shutil.copy2(dst, pre)
print("前像已存 : %s (%d 字节)" % (pre, os.path.getsize(pre)))

# 原子替换，与 tree_write.write_through 同规矩
tmp = dst + ".wt-tmp"
with open(tmp, "wb") as f:
    f.write(raw)
os.replace(tmp, dst)

got = open(dst, "rb").read()
print("修完     : %d 字节  盘==库 ? %s" % (len(got), got == raw))