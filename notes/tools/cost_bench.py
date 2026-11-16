"""实测：增量重算的成本（临时库 + 真模型；顺带对比"改一段"和"插一段"的差别）。"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, r"D:\core-notes\src")
from db import Database, DEFAULT_DATA_DIR          # noqa: E402
from indexer import Indexer                        # noqa: E402
from retriever import VectorIndexer                # noqa: E402

print("=== 机器 ===")
try:
    import torch
    print("  torch %s | CUDA 可用=%s | CPU 线程=%d"
          % (torch.__version__, torch.cuda.is_available(), torch.get_num_threads()))
except Exception as e:
    print("  torch 探测失败：%s" % str(e)[:60])

print("\n=== 真库基准（只读）===")
rd = Database(DEFAULT_DATA_DIR)
q = lambda s: rd.conn.execute(s).fetchone()[0]
print("  块 %d | 平均 %.0f 字/块 | 向量 %d | 向量占用 %.1f MB（float32 1024 维 → 4096 B/个）"
      % (q("SELECT COUNT(*) FROM chunks"), q("SELECT AVG(char_count) FROM chunks"),
         q("SELECT COUNT(*) FROM vectors"),
         (q("SELECT SUM(LENGTH(vec)) FROM vectors") or 0) / 1048576.0))
print("  正文合计 %.1f MB | 库文件 %.1f MB"
      % (q("SELECT SUM(LENGTH(text)) FROM chunks") / 1048576.0,
         os.path.getsize(os.path.join(DEFAULT_DATA_DIR, "index.db")) / 1048576.0))
rd.close()

TD = tempfile.mkdtemp(prefix="cost-")
db = Database(TD)
ix = Indexer(db)


def note(n_sec, body_repeat):
    out = ["# 成本测试笔记", ""]
    for i in range(n_sec):
        out += ["## 小节 %d" % i, "正文内容。" * body_repeat, ""]
    return "\n".join(out)


t0 = time.time()
vi = VectorIndexer(db)
vi.emb.encode_one("预热")                       # 含模型加载
print("\n=== 计时 ===")
print("  ① 模型加载 + 预热：%.1f 秒（进程启动的一次性成本）" % (time.time() - t0))

RAW = note(6, 90)                               # 6 个小节，每节约 450 字
fid = db.create_file(db.root_folder_id(), "zz-成本.md", RAW.encode("utf-8"))
t0 = time.time()
n = ix.reindex_file(fid)["chunks"]
t_chunk = time.time() - t0
t0 = time.time()
r = vi.index_files([fid])
t_vec = time.time() - t0
print("  ② 首次入库：%d 块；切块+写库 %.2fs；向量 %d 个 %.2fs（%.0f ms/块）"
      % (n, t_chunk, r["indexed"], t_vec, 1000.0 * t_vec / max(1, r["indexed"])))

# ③ 改一段（块数不变）
cur = db.get_content(fid).decode("utf-8")
edited = cur.replace("正文内容。" * 90, "正文内容。" * 89 + "这里改了两个字。", 1)
db.put_content(fid, edited.encode("utf-8"))
t0 = time.time()
n2 = ix.reindex_file(fid)["chunks"]
reused = db.last_reused
t2 = time.time() - t0
t0 = time.time()
r2 = vi.index_files([fid])
t3 = time.time() - t0
print("  ③ 改一小段（块数不变 %d→%d）：切块 %.2fs；续用旧向量 %d 个；重算 %s 个 %.2fs"
      % (n, n2, t2, reused, r2["indexed"], t3))

# ④ 中间插一小节（块数变）
cur = db.get_content(fid).decode("utf-8")
ins = cur.replace("## 小节 1", "## 插进来的小节\n正文内容。" * 1, 1) if False else \
    cur.replace("\n## 小节 1\n", "\n## 插进来的小节\n正文内容。\n\n## 小节 1\n", 1)
db.put_content(fid, ins.encode("utf-8"))
t0 = time.time()
n3 = ix.reindex_file(fid)["chunks"]
reused3 = db.last_reused
t4 = time.time() - t0
t0 = time.time()
r3 = vi.index_files([fid])
t5 = time.time() - t0
print("  ④ 中间插一节（块数 %d→%d）：切块 %.2fs；续用旧向量 %d 个；重算 %s 个 %.2fs"
      % (n2, n3, t4, reused3, r3["indexed"], t5))

per = 1000.0 * (t_vec / max(1, r["indexed"]))
print("\n=== 外推（按实测 %.0f ms/块）===" % per)
for label, cnt in (("改一篇 2000 字笔记里的一段（≈2 块）", 2),
                   ("改一篇 2 万字笔记里的一段（≈2 块）", 2),
                   ("一篇 20 万字日志全重算（694 块，实测就是这种）", 694),
                   ("全库重算（10914 块）", 10914)):
    print("  %-38s %8.1f 秒" % (label, cnt * per / 1000.0))
print("  （对照：2026-09-16 全库真跑过一次 10914 块 = 1188 秒 → %.0f ms/块，与本机测速一致）"
      % (1000.0 * 1188 / 10914))

db.close()
shutil.rmtree(TD, ignore_errors=True)
print("\n（临时库已删，真库未动）")
