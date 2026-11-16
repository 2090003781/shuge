"""阶段性体检：把 tools/ 下每个脚本跑一遍，汇总通过/失败。

只报告，不改动。输出：每个脚本的退出码 + 末几行；最后给失败清单。
"""
import glob
import io
import json
import os
import subprocess
import sys
import time

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PY = [("python", "tools/smoke.py"), ("python", "tools/smoke_api.py"),
      ("python", "tools/smoke_entry.py"), ("python", "tools/smoke_pool.py"),
      ("python", "tools/smoke_import.py"), ("python", "tools/smoke_files.py"),
      # 新建文件的格式是**显式规定**的（format：md/txt/docx…；名字没扩展名默认 md）2026-10-03
      ("python", "tools/smoke_create_format.py"),
      # 正文**追加**（POST /api/files/<id>/append）：库内接在尾部、盘上按字节追加 2026-10-04
      ("python", "tools/smoke_append.py"),
      # 统一批量入口 POST /api/batch（白名单 apidoc.BATCH_OPS；内部转发到单条路由）2026-10-04
      ("python", "tools/smoke_batch.py"),
      ("python", "tools/smoke_sections.py"),
      # 档位还原：跑平台自测不许把主人设的档位留下（子进程被 kill 时父进程兜底）2026-10-04
      ("python", "tools/smoke_perm_restore.py"),
      ("python", "tools/smoke_content.py"), ("python", "tools/smoke_refs.py"),
      ("python", "tools/smoke_rerank.py"), ("python", "tools/smoke_embed_norm.py"),
      ("python", "tools/smoke_mentions.py"),
      ("python", "tools/smoke_tags.py"), ("python", "tools/smoke_time.py"), ("python", "tools/smoke_article.py"),
      ("python", "tools/smoke_convert_refs.py"),
      ("python", "tools/smoke_section.py"), ("python", "tools/smoke_pathref.py"),
      ("python", "tools/smoke_snapshot.py"),
      ("python", "tools/smoke_selfcheck.py"),
      ("python", "tools/smoke_doc.py"),
      ("python", "tools/smoke_blocks.py"),
      ("python", "tools/smoke_incremental.py"),
      # 私密加锁：对着临时库跑（它要在库上临时设密码再还原；真库旁边站着投递箱，
      # 库一解锁它就会用测试密码把 个人/私密 加密进库 → kdf 一还原就永远解不开）（2026-09-22）
      ("python", "tools/run_smoke_lock.py"),
      # 名字逃逸回归：库文件夹不能被「名字」甩出去（2026-09-30 体检 H1/H2）
      ("python", "tools/smoke_name_guard.py"),
      # 按格式切编辑器：PDF 页清单/渲染/查找 + 文档回写（临时库）2026-09-30
      ("python", "tools/smoke_editors.py"),
      # PDF 引擎：文字层/批注写回（留前像）/页面操作/该拒绝的（临时库）2026-09-30
      ("python", "tools/smoke_pdfeng.py"),
      # PDF 引擎离线段：擦字重画/替换/印记/合并/导出图/扁平化（不启库，2026-10-01）
      ("python", "tools/acc_pdfeng_edit.py"),
      # DOCX 引擎：OOXML 级读写（改字不丢样式）—— 离线段 + 接口段（2026-10-01）
      ("python", "tools/acc_docxeng.py"),
      ("python", "tools/smoke_docxeng.py"),
      # 并发：读并发、写串行（慢写在飞时轻小请求照样回；写与写仍然串行；标签不串味）2026-10-01
      ("python", "tools/smoke_threading.py"),
      # 会话超时：默认**不自动上锁**，设了 AI_NOTES_LOCK_TIMEOUT 才开（2026-09-28）
      ("python", "tools/smoke_lock_idle.py"),
      ("python", "tools/smoke_export.py"), ("python", "tools/smoke_events.py"),
      ("python", "tools/verify_migrate.py"),
      ("python", "tools/api_spec.py --check"),
      # 密钥材料不许从 /api/health 漏出去（2026-09-30 体检 ③）
      ("python", "tools/smoke_health_secret.py"), ("python", "tools/smoke_brain_plugin.py"),
      ("python", "tools/check_ui.py"),
      ("python", "tools/check_ui_paths.py"), ("python", "tools/check_cors.py"),
      ("python", "tools/check_proxy.py"), ("python", "tools/diag_http.py"),
      ("python", "tools/smoke_rag.py"),
      # 文档线第 1 步：按磁盘路径索引进库 + 批量进/出检索（2026-09-21）
      ("python", "tools/smoke_index_rag.py"),
      # 表格线第 1 步：csv/tsv 建表 + 只读查询（2026-09-21）
      ("python", "tools/smoke_tables.py"),
      # 表格线第 2 步：xlsx → 表（多 sheet 多表，零依赖读取）
      ("python", "tools/smoke_tables_xlsx.py"),
      # 投递箱 v1.1：同名文件改了要跟上（磁盘新→覆盖，库里新→kept）（2026-09-22）
      ("python", "tools/smoke_import_overwrite.py"),
      # 私密进库：照进库但加密 + 锁定态读不到搜不到（2026-09-22）
      ("python", "tools/smoke_private_import.py"),
      # 回收站：删除留前像 / 清单 / 恢复 / 清理旧前像（2026-09-28 界面加回入口）
      ("python", "tools/smoke_bin.py"),
      # 入库自动转换：磁盘留 [[目标]]，进库按库规则转 @目标（2026-09-22）
      ("python", "tools/smoke_import_refs.py"),
      # 写入者标签 + 高风险前像（删除/覆盖可恢复）（2026-09-24）
      ("python", "tools/acc_writer_tags.py"),
      # 查询缓存 + 向量缓存增量刷新（写完立刻可检索，不整表重载）（2026-09-25）
      # 注：tools/acc_cold.py 会重启库，不能进这个合集（它单独跑）
      ("python", "tools/acc_cache.py"),
      # 前置属性（Obsidian "属性"）：导入方向 + 写回 YAML + 双向对贴（2026-09-27）
      ("python", "tools/smoke_frontmatter.py"),
      # "库改了磁盘必须跟着改"：改名带出的引用改写 / 文件夹改名 / 产物落盘 / 删文件夹（2026-09-27 审计 A B C E）
      ("python", "tools/acc_write_through.py"),
      # 前置属性解析的容错：@ / [[ ]] 开头的值不能整块丢，解析失败不许清空（2026-09-27 审计）
      ("python", "tools/smoke_fm_parse.py"),
      # 上锁隐藏 / 解锁显示私密篇（辞玖 2026-09-27）
      ("python", "tools/smoke_private_hide.py"),
      # 导入改成"从系统选择框选路径"（协议验收，不弹窗）2026-09-28
      ("python", "tools/smoke_pick.py"),
      # 浏览器选文件 → 按字节导入（不再手打路径）2026-09-28
      ("python", "tools/smoke_import_upload.py"),
      # 属性索引 / 按属性找 / 改属性写回 YAML（右侧栏用）2026-09-27
      ("python", "tools/smoke_properties.py"),
      # 库文件夹 ⇄ vault 双向同步（谁新谁赢、忽略项、私密前缀跳过）（2026-09-22）
      ("node", "tools/verify_editor.cjs"), # 命中位置验收：**当场搜**（不再用抓取快照当夹具 —— 那种文件含真 file_id/真片段，2026-10-06 清掉）
      ("node", "tools/verify_hit_locate.cjs"),
      ("node", "tools/smoke_ui.cjs"), ("node", "tools/smoke_ui_prefix.cjs"),
      # 界面"状态跟不跟得上"：保存/删除/连点/改标签/本篇搜索/文档 的联动与复位（2026-09-27 审计）
      ("node", "tools/verify_uistate.cjs"),
      ("node", "tools/debug_ui_render.cjs"),
      # 控制台权限块：桩 DOM 跑真接口，验「视觉 = 实际」（改档不回填/连点写乱）
      ("node", "tools/verify_perm_ui.mjs")]

# ── 开跑前先扫掉上次崩溃留下的 zz- 临时笔记/文件夹 ────────────
# （2026-09-25：一次 smoke_mentions 中途崩了，留下同名的 zz- 目标，
#  后面 smoke_mentions / smoke_pathref 都把它当“真笔记”选中 → 一连串假失败）
def sweep_zz():
    import urllib.request

    BASE = "http://127.0.0.1:8848"

    def req(method, path):
        r = urllib.request.Request(BASE + path, data=b"" if method == "DELETE" else None,
                                   method=method, headers={"X-Writer": "tool:sweep"})
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    gone = 0
    try:
        tree = req("GET", "/api/tree")["tree"]
        files, folders = [], []

        def walk(n, depth):
            for f in n.get("files", []):
                if str(f.get("name", "")).startswith("zz-"):
                    files.append(f["id"])
            for c in n.get("children", []):
                if str(c.get("name", "")).startswith("zz-") and c.get("id"):
                    folders.append((depth + 1, c["id"]))
                walk(c, depth + 1)
        walk(tree, 0)
        for fid in files:
            try:
                req("DELETE", "/api/files/" + fid)
                gone += 1
            except Exception:
                pass
        for _, fid in sorted(folders, reverse=True):      # 先深后浅（只能删空文件夹）
            try:
                req("DELETE", "/api/folders/" + fid)
                gone += 1
            except Exception:
                pass
    except Exception as e:
        print("（zz- 残留清理跳过：%s）" % str(e)[:80])
        return
    if gone:
        print("（开跑前清了 %d 个 zz- 测试残留）" % gone)


sweep_zz()


# ── 档位护栏（2026-10-04）──────────────────────────────────
# 有的自测会改平台侧 agent 接口的档位（控制台权限块自测、MCP 面自测），
# 而"测试改过的东西"绝不该留在主人的设置里 —— 总入口记一笔，跑完无条件还原。
_PERM_PLUGIN = "plugin.dsh"


def _perm_level():
    try:
        st, d = req("GET", "/api/permissions?plugin=" + _PERM_PLUGIN)
        return (d or {}).get("level") or ""
    except Exception:
        return ""


def _perm_set(lv):
    try:
        req("POST", "/api/permissions/set", {"plugin": _PERM_PLUGIN, "level": lv})
        return True
    except Exception:
        return False


_perm_before = _perm_level()
if _perm_before:
    print("（开跑前的 %s 档位：%s —— 跑完会核对并还原）" % (_PERM_PLUGIN, _perm_before))

results = []
for cmd, rel in PY:
    path = os.path.join(ROOT, rel.split()[0].replace("/", os.sep))
    if not os.path.isfile(path):
        results.append((rel, "缺文件", "", 0.0))
        continue
    t0 = time.time()
    try:
        p = subprocess.run([cmd] + rel.split(), cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1200, creationflags=NO_WINDOW)
        # 失败时多留几行上下文（只留最后 3 行常常看不到"哪一条 ❌"，2026-09-22 smoke_article 就是这样）
        keep = 3 if p.returncode == 0 else 15
        tail = "\n".join([x for x in (p.stdout or "").strip().splitlines()[-keep:]])
        err = "\n".join([x for x in (p.stderr or "").strip().splitlines()[-2:]])
        results.append((rel, "OK" if p.returncode == 0 else "FAIL(%d)" % p.returncode,
                        (tail + ("\n   stderr: " + err if err and p.returncode else "")), time.time() - t0))
    except subprocess.TimeoutExpired:
        results.append((rel, "超时", "", time.time() - t0))
    except Exception as e:
        results.append((rel, "异常", str(e), time.time() - t0))

# 跑完：档位被哪个自测改过就还回去（主人的设置不许被测试留下脚印）
if _perm_before:
    _perm_after = _perm_level()
    if _perm_after and _perm_after != _perm_before:
        ok_back = _perm_set(_perm_before)
        print("↩︎ %s 档位被某个自测从 %s 改成了 %s —— %s"
              % (_PERM_PLUGIN, _perm_before, _perm_after,
                 ("已还原" if ok_back else "⚠️ 还原失败，请手动设回")))
    else:
        print("（%s 档位一路没被动过：%s）" % (_PERM_PLUGIN, _perm_before))

print("=" * 78)
for rel, status, tail, secs in results:
    mark = "✅" if status == "OK" else "❌"
    print("%s %-40s %-10s %5.1fs" % (mark, rel.split()[0], status, secs))
    if status != "OK" and tail:
        for line in tail.splitlines():
            print("      " + line[:160])
# 清掉测试造出来的墓碑（zz- 开头的临时笔记；真实删除记录不动）
try:
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from db import Database
    d = Database()
    n = d.conn.execute("DELETE FROM tombstones WHERE display LIKE 'zz-%'").rowcount
    d.conn.commit()
    d.close()
    if n:
        print("（顺手清了 %d 条测试墓碑）" % n)
except Exception as e:
    print("（测试墓碑清理跳过：%s）" % str(e)[:80])

# 顺手清掉测试造出来的 zz- 前像（高风险前像目录只该留真事）
try:
    idx = os.path.join(ROOT, "data", "前像", "index.jsonl")
    if os.path.isfile(idx):
        keep, gone = [], 0
        for line in io.open(idx, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            # zz- 名字是测试夹具；writer 形如 "auto:xxx" = 发起方**没带 X-Writer**。
            # 页面/agent/任务/工具都会带自己的标签，所以"没人认领"的写入就是测试残留（2026-09-28）
            if ((rec.get("name") or "").startswith("zz-")
                    or str(rec.get("writer") or "").startswith("auto:")):
                p = os.path.join(ROOT, "data", rec.get("image", "").replace("/", os.sep))
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                except OSError:
                    pass
                gone += 1
            else:
                keep.append(rec)
        with io.open(idx, "w", encoding="utf-8") as f:
            for rec in keep:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if gone:
            print("（顺手清了 %d 条测试前像）" % gone)
except Exception as e:
    print("（测试前像清理跳过：%s）" % str(e)[:80])

print("=" * 78)
bad = [r for r in results if r[1] != "OK"]
print("通过 %d / %d；失败：%s" % (len(results) - len(bad), len(results),
                                "、".join(r[0] for r in bad) or "无"))
sys.exit(1 if bad else 0)
