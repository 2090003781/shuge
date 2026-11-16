#!/usr/bin/env python3
"""上下文包插件（external）——只负责"按配置准备包"，不做注入。

职责划分（辞玖 2026-09-10 定稿）：
  本插件 = 包的生产者/供货方：按路由配置组装注入包，写 data/context/<agent>.md
  平台 agent 接口 = 注入的执行者：会话开始直读文件注入（零转接）

包内容 = 两段相加（2026-09-13 加第二段）：
  ① 路由文档：每个 agent 一份清单（config 的 docs 关键字为默认），控制台可改可增删，
     存 data/context/routes-docs.json（覆盖 config 默认）
  ② 命名包：控制台里"新建的包"（自己起名 + 挑 Vault 文件 + 指定给哪些 agent），
     存 data/context/packs.json；同样可增删改
  两种清单统一形态：[{path, enabled}] —— path 是 Vault 相对路径（路由文档还允许
  persona/profile/rules-l1 关键字），enabled 支持**单文件级启停**（停用=保留条目不进包）

特点：
  · 包是"缓存"：刷新时机 = 事件触发 + 定时兜底 + 包变更，注入方永远直读现成文件
  · 没有"挂不挂"的概念：进程停了包还在，注入照常
  · 可用性由开关控制（插件级启停 + agent 级 enabled + 包级 enabled，控制台可切换）
  · 每轮从平台取路由配置（/api/context/routes）→ 只为 enabled 的 agent 组装包

行为：
  启动 → 注册身份 → 立即组装一轮 → 启动源文件监听 → 进入等待
  刷新时机（三条，2026-09-13 补齐第三条）：
    ① 事件驱动：包源文件一变即刻重新组包
       （watchdog → Windows ReadDirectoryChangesW：平时挂在等待上零 CPU，一变就醒）
    ② 定时兜底：每 --interval 秒（默认 300）无条件刷新一次
    ③ 包变更：pack.create/delete/update 落地后立即重组（并重建监听目录）
  父进程（core）消失 → 5 秒内自行退出（防孤儿）
  收到 SIGTERM/SIGINT → 优雅退出

工具（平台可点名调用，能力名 == 工具名）：
  context.get / pack.list / pack.create / pack.delete / pack.update / pack.preview
  pack.entries（读/改某 agent 的路由文档清单）/ pack.set-file（单文件启停）

依赖：标准库 + watchdog（仅用于 ①）。未安装时自动降级为「仅定时刷新」，功能不中断。
"""
import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.request

# 分账助手（plugins/lib/detail_ledger.py）：总账记"状态"，分账记"详情"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from detail_ledger import DetailLedger  # noqa: E402
from probe import Probe, auth_headers  # noqa: E402

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    HAS_WATCHDOG = True
except ImportError:                      # 降级：只用定时刷新
    HAS_WATCHDOG = False

CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:8790")
VAULT = os.environ.get("VAULT_PATH", "D:/个人数据/辞玖")
PLUGIN_ID = "plugin.context-pack"

running = True

# 文档关键字 → Vault 相对路径（路由 docs 里写关键字即可）
DOC_SOURCES = {
    "persona": os.path.join("second-brain-system", "dsh", "DSH-人设.md"),
    "profile": os.path.join("个人", "画像", "用户画像-核心.md"),
    "rules-l1": os.path.join("记忆", "原则区.md"),
}
DOC_TITLES = {"persona": "人设", "profile": "用户画像（核心）", "rules-l1": "规则（1 级摘要）"}

# ── 命名注入包（新建/删除，可选文件）2026-09-13 加 ─────────────
# 背景：原来一个 agent 的包内容只能由平台路由（config 里的 docs 关键字）决定，
# 想把某个笔记塞进上下文就得改平台配置。现在插件自己管"包"：
#   data/context/packs.json = [{id, name, files[], agents[], enabled, created_at}]
#     files  = Vault 相对路径（控制台从 /api/vault/list 的树里挑）
#     agents = 目标 agent（空 = 所有启用注入的 agent）
#     enabled= 开关（关掉 = 不参与组包，但不删）
# 组装规则：路由默认 docs（人设/画像/规则）+ 命中该 agent 的所有启用包
MAX_PACK_FILES = 20        # 单个包的文件数上限（防手滑选整棵树）
PACK_FILE_CHARS = 6000     # 单文件截断（与 read_doc 一致）
PACK_TOTAL_CHARS = 40000   # 单个 agent 的"包内容"总量上限


def packs_path():
    """包清单路径：随包目录（CONTEXT_DIR），可用 PACKS_FILE 覆盖（演练用）"""
    return os.environ.get("PACKS_FILE") or os.path.join(context_dir(), "packs.json")


def load_packs():
    """读命名包清单（任何异常 → 空清单：绝不阻断组包）"""
    try:
        with open(packs_path(), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[ctx-pack] 读包清单失败（当作空）: {e}", flush=True)
        return []
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, dict) and p.get("id")]


def save_packs(packs):
    """原子写包清单（先写临时文件再替换，避免留下半截文件）"""
    path = packs_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(packs, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ── 路由默认文档的可编辑覆盖（2026-09-13 加）──────────────────
# 背景：原来路由 docs 是 config 里的硬编码关键字，控制台改不了。现在把「每个 agent 的
# 路由文档清单」也交到控制台：存 data/context/routes-docs.json
#   {"agent.dsh": [{"path": "second-brain-system/dsh/DSH-人设.md", "enabled": true}, ...]}
# 关键字（persona/profile/rules-l1）依旧合法（插件内映射到 DOC_SOURCES）；文件路径同样合法。
# 没覆盖的 agent → 用平台路由（config）给的关键字。
def routes_docs_path():
    return os.environ.get("ROUTES_DOCS_FILE") or os.path.join(context_dir(), "routes-docs.json")


def load_route_docs():
    try:
        with open(routes_docs_path(), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[ctx-pack] 读路由文档覆盖失败（当作空）: {e}", flush=True)
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for agent, files in data.items():
        if agent == "_defaults":
            continue
        if isinstance(files, list):
            out[str(agent)] = norm_files(files)
    return out


def load_route_defaults():
    """平台 config 的默认文档快照：第一次做覆盖时记下来，供「恢复默认」用

    不能直接读 GET /api/context/routes：那个接口返回的是**当前生效**的 docs
    （可能已经是我们覆盖后的值），拿它当"默认"会把覆盖又写回去。
    """
    try:
        with open(routes_docs_path(), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    defaults = data.get("_defaults") if isinstance(data, dict) else None
    if not isinstance(defaults, dict):
        return {}
    return {str(k): norm_files(v) for k, v in defaults.items() if isinstance(v, list)}


def _save_route_docs_raw(store, defaults):
    path = routes_docs_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = dict(store)
    if defaults:
        payload["_defaults"] = defaults
    names = load_folder_names()          # 保留文件夹显示名（2026-09-13）
    if names:
        payload["_names"] = names
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def save_route_docs(store, defaults=None):
    _save_route_docs_raw(store, defaults if defaults is not None else load_route_defaults())


def load_folder_names():
    """agent 文件夹的显示名（默认包的名字）——存 routes-docs.json 的 _names（2026-09-13 加）"""
    try:
        with open(routes_docs_path(), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    names = data.get("_names") if isinstance(data, dict) else None
    if not isinstance(names, dict):
        return {}
    return {str(k): str(v) for k, v in names.items() if str(v).strip()}


def save_folder_names(names):
    try:
        with open(routes_docs_path(), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["_names"] = dict(names or {})
    path = routes_docs_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def route_doc_files():
    """所有 agent 路由文档里用到的 Vault 相对路径（关键字映射成真实路径）"""
    out = []
    for files in load_route_docs().values():
        for rel in enabled_files(files):
            out.append(DOC_SOURCES.get(rel, rel))
    return out


def doc_label(key):
    """关键字 → 显示名（文件路径原样返回）"""
    return DOC_TITLES.get(key, key)


def norm_rel(rel):
    """Vault 相对路径规范化；非法（空/绝对路径/上跳）→ 返回 None"""
    s = str(rel or "").replace("\\", "/").strip().lstrip("/")
    if not s or os.path.isabs(s) or ".." in s.split("/"):
        return None
    return s


# ── 外部文件（Vault 之外）支持 2026-09-13 加 ──────────────────
# 背景：辞宝要往包里放 Vault 之外的文件。Web 控制台无法"浏览选择"本地文件
# （浏览器不给绝对路径），所以只能**手填绝对路径**。
# 护栏（本机单人平台，但防止误配把不该读的东西读进模型上下文）：
#   · 只读普通文件（isfile），目录/设备不读
#   · 单文件仍受 PACK_FILE_CHARS 截断（不会把大文件整份塞进上下文）
#   · 拒读密钥类路径（.credentials / token / .env / id_rsa / .git 内部）
_SECRET_HINTS = ("token.json", ".credentials", ".env", "id_rsa", "id_ed25519",
                 "/.git/", "\\/.git\\", ".pem", ".key", "credentials.yaml")
MAX_EXTERNAL_BYTES = 2 * 1024 * 1024      # 2MB 上限（读之前先看 size，避免读爆）


def is_abs_path(p):
    """是否是外部绝对路径（Windows 盘符 / UNC / POSIX）"""
    s = str(p or "").strip()
    if not s:
        return False
    return os.path.isabs(s) or (len(s) > 2 and s[1] == ":")


def external_forbidden(p):
    """外部路径的拒读名单（密钥/凭据类）"""
    low = str(p or "").replace("\\", "/").lower()
    return any(h.replace("\\", "/") in low for h in _SECRET_HINTS)


def resolve_path(rel):
    """条目路径 → 实际文件路径。

    Vault 相对路径 → 拼到 VAULT 下；外部绝对路径 → 原样使用（2026-09-13 加）。
    返回 (绝对路径, 错误信息)；不可用时错误信息非空。
    """
    s = str(rel or "").strip()
    if not s:
        return "", "空路径"
    if is_abs_path(s):
        if external_forbidden(s):
            return "", "该路径在拒读名单里（密钥/凭据类）"
        try:
            if not os.path.isfile(s):
                return "", "文件不存在"
            if os.path.getsize(s) > MAX_EXTERNAL_BYTES:
                return "", f"外部文件超过 {MAX_EXTERNAL_BYTES // 1024 // 1024}MB 上限"
        except Exception as e:
            return "", str(e)
        return s, ""
    r = norm_rel(s)
    if r is None:
        return "", "非法路径"
    path = os.path.join(VAULT, r)
    if not os.path.isfile(path):        # 【补】相对路径同样要判存在性
        return "", "文件不存在"
    return path, ""


def file_entry(rel, enabled=True):
    """统一文件条目形态 {path, enabled}——2026-09-13 加：单文件级启停。

    兼容：字符串（老格式 / 控制台简单传参）→ 自动升级为启用态条目。
    """
    if isinstance(rel, dict):
        p = str(rel.get("path") or rel.get("rel") or "")
        return {"path": p, "enabled": rel.get("enabled", True) is not False}
    return {"path": str(rel or ""), "enabled": enabled is not False}


def norm_files(files):
    """把清单里混杂的字符串/条目统一成条目数组（保序、按 path 去重，后者覆盖前者）"""
    out, seen = [], {}
    for item in files or []:
        e = file_entry(item)
        k = e["path"]
        if k in seen:
            out[seen[k]] = e
            continue
        seen[k] = len(out)
        out.append(e)
    return out


def paths_of(files):
    """取清单里的路径字符串（老调用面用）"""
    return [e["path"] for e in norm_files(files)]


def enabled_files(files):
    """只要启用态的文件路径"""
    return [e["path"] for e in norm_files(files) if e.get("enabled", True)]


def missing_files(files):
    """列出启用态但 Vault 里找不到的路径。

    注意：路由文档允许**关键字**（persona/profile/rules-l1），它们不是路径——
    修复（2026-09-13）：关键字直接跳过，否则控制台会对合法条目误报"找不到"（实测踩到）。
    """
    out = []
    for rel in enabled_files(files):
        if rel in DOC_SOURCES:
            continue
        okk, _err = entry_exists(rel)
        if not okk:
            out.append(rel)
    return out


def missing_reason_text(missing):
    """把不可用条目渲染成带原因的提示（区分：拒读名单 / 不存在 / 非法）"""
    if not missing:
        return ""
    parts = []
    for p in missing[:5]:
        _okk, err = entry_exists(p)
        parts.append(f"{p}（{err or '库里和磁盘上都没有'}）")
    more = f" 等 {len(missing)} 项" if len(missing) > 5 else ""
    return "、".join(parts) + more


def validate_files(files):
    """校验文件清单：返回 (可用条目清单, 启用态里不可用的路径清单)

    条目可以是：关键字（persona/profile/rules-l1）、Vault 相对路径、**外部绝对路径**（2026-09-13 加）。
    全部走 resolve_path 统一判定（相对/绝对、存在性、拒读名单）。
    """
    ok, missing, seen = [], [], set()
    for e in norm_files(files):
        p = e["path"]
        if e.get("enabled", True) is False:
            continue                                  # 停用条目不参与校验
        if p in DOC_SOURCES:                          # 关键字：不查盘
            if p not in seen:
                seen.add(p)
                ok.append({"path": p, "enabled": True})
            continue
        okk, err = entry_exists(p)
        if not okk:
            missing.append(p)
            continue
        r = norm_rel(p) if not is_abs_path(p) else p
        if r in seen:
            continue
        seen.add(r)
        ok.append({"path": r, "enabled": True})
    return ok, missing


# ── 内容来源：库优先，磁盘兜底（2026-09-14 辞玖定：库是主体/真相源）──────
#   包里的条目写"库内路径"（如 vault/项目/待办.md）；老写法（Vault 相对路径 项目/待办.md）
#   库侧也认（会自动补 vault/ 前缀）。库不可达/库里没有 → 退回读 Vault 磁盘，包不至于空。
NOTES_URL = (os.environ.get("AINOTES_URL") or "http://127.0.0.1:8848").rstrip("/")
NOTES_TIMEOUT = float(os.environ.get("AINOTES_TIMEOUT") or 8)
_lib_cache = {}          # rel -> item（组包一轮内复用；带 TTL）
_lib_cache_ttl = 5.0
_lib_down_until = [0.0]  # 库不可用时短暂退避，别每条都等满超时
LIB_STAT = {"lib": 0, "vault": 0}   # 本次组包内容分别来自哪（可观测）


def _lib_post(path, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        NOTES_URL + path, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=NOTES_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def library_items(paths, max_chars=0, refresh=False):
    """批量问库（{rel: item}）；库不可达返回 ({}, 错误)。列表为空则不发请求。"""
    paths = [p for p in paths if p and not is_abs_path(p)]
    if not paths:
        return {}, ""
    now = time.time()
    out, todo = {}, []
    for p in paths:
        hit = _lib_cache.get(p)
        if hit and not refresh and now - hit[0] < _lib_cache_ttl:
            out[p] = hit[1]
        else:
            todo.append(p)
    if todo and now >= _lib_down_until[0]:
        try:
            r = _lib_post("/api/content/batch", {"paths": todo, "max": max_chars})
            for p, item in (r.get("items") or {}).items():
                _lib_cache[p] = (now, item)
                out[p] = item
        except Exception as e:
            _lib_down_until[0] = now + 30.0     # 30 秒内不再试，直接走磁盘
            return out, "库不可达：%s" % str(e)[:80]
    elif todo:
        return out, "库暂不可用（退避中）"
    return out, ""


def library_item(rel, max_chars=0, refresh=False):
    """单条问库；返回 (item|None, err)"""
    items, err = library_items([rel], max_chars=max_chars, refresh=refresh)
    return items.get(rel), err


def entry_exists(rel):
    """条目是否可用：**库里有 || 磁盘上有**。返回 (ok, 原因)

    口径：库是真相源（2026-09-27 起 vault 只读参照），所以「只在库里、磁盘上没有」的条目
    （如 `vault/个人/画像/用户画像-核心.md`）算**可用**；`vault/` 前缀是**库内路径**，不是 vault 相对路径。
    """
    if rel in DOC_SOURCES:
        return True, ""
    lerr = ""
    if not is_abs_path(rel):
        item, lerr = library_item(rel)
        if not lerr and item and item.get("found"):
            return True, ""
    _, e = resolve_path(rel)
    if e and lerr:
        # 磁盘没有、库那边也没问到 → 说清是"问不到"，别冒充"文件不存在"（2026-10-01）
        e = "%s（库侧也没问到：%s）" % (e, lerr)
    return (not e), e


def read_vault_file(rel):
    """读条目内容：**先问库（真相源）**，库不可达/库里没有才退 Vault 磁盘。返回 (text, err)"""
    if not is_abs_path(rel):
        if external_forbidden(rel):
            return "", "该路径在拒读名单里（密钥/凭据类）"
        item, err = library_item(rel, max_chars=PACK_FILE_CHARS)
        if not err and item is not None and item.get("found"):
            if item.get("locked"):
                return "", "该笔记在库里已加密上锁（先在库里解锁再组包）"
            text = item.get("content") or ""
            if item.get("truncated"):
                text += "\n…（已截断，原文 %d 字符）" % item.get("chars", len(text))
            LIB_STAT["lib"] += 1
            return text, ""
    path, err = resolve_path(rel)
    if err:
        return "", err
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        return "", str(e)
    LIB_STAT["vault"] += 1
    if len(text) > PACK_FILE_CHARS:
        return text[:PACK_FILE_CHARS] + f"\n…（已截断，原文 {len(text)} 字符）", ""
    return text, ""


def packs_for_agent(agent, packs=None):
    """命中该 agent 的启用包（agents 为空 = 所有 agent）"""
    out = []
    for p in (packs if packs is not None else load_packs()):
        if not p.get("enabled", True):
            continue
        agents = p.get("agents") or []
        if agents and agent not in agents:
            continue
        out.append(p)
    return out


# ── 事件驱动刷新 ────────────────────────────────────────────
# 编辑器保存一个文件常触发多个事件（写临时文件 → 改名覆盖），
# 因此不立即组包，而是标记"待刷新"，DEBOUNCE_SECONDS 内的事件合并成一次。
DEBOUNCE_SECONDS = 0.8
_pending = {"dirty": False, "due": 0.0}
_stats = {"event_refresh": 0, "timer_refresh": 0}


def watched_names():
    """需要监听的源文件名：基础文档 + 所有命名包引用的文件（包可变 → 每次现算）"""
    names = {os.path.basename(rel) for rel in DOC_SOURCES.values()}
    for rel in route_doc_files():
        s = norm_rel(rel)
        if s:
            names.add(os.path.basename(s))
    for p in load_packs():
        for rel in enabled_files(p.get("files")):
            s = norm_rel(rel)
            if s:
                names.add(os.path.basename(s))
    names.discard("")
    return names


def watch_dirs():
    """需要监听的目录：基础文档所在目录 + 命名包文件所在目录"""
    rels = list(DOC_SOURCES.values()) + route_doc_files()
    for p in load_packs():
        rels.extend(enabled_files(p.get("files")))
    dirs = set()
    for rel in rels:
        s = norm_rel(rel)
        if s:
            dirs.add(os.path.dirname(os.path.join(VAULT, s)))
    return sorted(dirs)


# ── 父进程看护（2026-09-10 加）───────────────────────────────
# 背景：core 被强杀时（reasonix job 回收 / taskkill）没机会清理子进程，
# 插件会变成「孤儿」继续跑 —— 实测曾堆积 6 个孤儿，多个实例互相覆盖写同一个包。
# 对策：每 PARENT_CHECK_SECONDS 秒确认父进程还活着，父没了就自行退出。
# 零依赖：Windows 用 kernel32 API，其他平台回退 os.kill(pid, 0)。
PARENT_CHECK_SECONDS = 5
_PARENT_PID = os.getppid()


def _pid_alive(pid):
    """判断进程是否存活。"""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


class _SourceHandler(FileSystemEventHandler):
    """只关心包源文件的变化，其余文件（同目录下的其他笔记）一律忽略。"""

    def _mark(self, path):
        if not path:
            return
        if os.path.basename(path) not in watched_names():
            return
        _pending["dirty"] = True
        _pending["due"] = time.time() + DEBOUNCE_SECONDS

    def on_modified(self, event):
        self._mark(event.src_path)

    def on_created(self, event):
        self._mark(event.src_path)

    def on_deleted(self, event):
        self._mark(event.src_path)

    def on_moved(self, event):
        # 编辑器原子保存：临时文件 moved 到目标名 → 看 dest
        self._mark(getattr(event, "dest_path", "") or event.src_path)


# 监听器句柄（包变更后要重建：新挑的文件目录也得能事件触发）
_OBSERVER = {"obs": None}


def start_source_watchers():
    """监听包源文件所在目录（事件驱动，平时休眠零开销）。返回 Observer 或 None。"""
    if not HAS_WATCHDOG:
        print("[ctx-pack] 未安装 watchdog → 仅定时刷新模式", flush=True)
        return None

    observer = Observer()
    handler = _SourceHandler()
    scheduled = []
    for d in watch_dirs():
        if os.path.isdir(d):
            observer.schedule(handler, d, recursive=False)
            scheduled.append(d)
        else:
            print(f"[ctx-pack] 源目录不存在，跳过监听：{d}", flush=True)
    if not scheduled:
        return None
    observer.daemon = True
    observer.start()
    print(f"[ctx-pack] 已监听 {len(scheduled)} 个源目录（事件驱动）："
          f"{', '.join(os.path.basename(d) for d in scheduled)}", flush=True)
    return observer


def restart_source_watchers():
    """重建监听目录（包清单变了：新挑的文件所在目录也要纳入事件驱动）"""
    old = _OBSERVER.get("obs")
    if old is not None:
        try:
            old.stop()
            old.join(timeout=2)
        except Exception as e:
            print(f"[ctx-pack] 停旧监听失败（忽略）: {e}", flush=True)
        _OBSERVER["obs"] = None
    _OBSERVER["obs"] = start_source_watchers()
    return _OBSERVER["obs"]


def handle_stop(signum, frame):
    global running
    running = False


def http_post(path, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(CORE_URL + path, data=body,
                                headers=auth_headers({"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def http_get(path):
    with urllib.request.urlopen(CORE_URL + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _doc_text(rel):
    """一段路由文档的正文：**先问库（真相源）**，库里没有 / 库不可达才退 Vault 磁盘。

    2026-09-30 体检修的就是这里：原来只 `open(VAULT/rel)`。自从 09-27「库成为唯一真相源」，
    只在库里存在的文档（`记忆/原则区.md` 就是）**永远读不到** —— `agent.dsh` 的注入包
    从那天起一直静默少一段「原则区 1 级规则」，不报错、没人发现。
    这里要**全文**（不截断）：rules-l1 得先从 5.5 万字里挑出 1 级摘要。
    """
    item, err = library_item(rel, max_chars=0)
    if not err and item is not None and item.get("found"):
        if item.get("locked"):
            return ""
        return item.get("content") or ""
    try:
        with open(os.path.join(VAULT, rel), encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def read_doc(key):
    """取一段路由文档：关键字（persona/profile/rules-l1）或 Vault 相对路径。

    rules-l1 保留"只取 1 级摘要"的特殊处理；其余按普通文件读（截断 PACK_FILE_CHARS）。
    内容来源：**库优先、vault 兜底**（见 `_doc_text`）。
    """
    is_key = key in DOC_SOURCES
    rel = DOC_SOURCES.get(key, key)
    s = norm_rel(rel)
    if s is None:
        return ""
    text = _doc_text(s)
    if not text:
        return ""
    if is_key and key == "rules-l1":
        return extract_level1(text)
    if len(text) > PACK_FILE_CHARS:
        return text[:PACK_FILE_CHARS] + f"\n…（已截断，原文 {len(text)} 字符）"
    return text


def extract_level1(text):
    """从原则区提取 1 级规则要点（原则/冲突裁决/状态行）"""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end > 0:
            text = text[end + 3:]
    lines, keep = [], False
    for raw in text.split("\n"):
        s = raw.strip()
        if s.startswith("## "):
            keep = True
            lines.append("\n" + s)
            continue
        if s.startswith("# "):
            keep = False
            continue
        if keep and (s.startswith("- **原则：") or s.startswith("- **冲突裁决：")
                     or s.startswith("- **状态：") or s.startswith("- **锚定：")):
            lines.append(s)
    return "\n".join(lines) or "(未提取到 1 级规则)"


def build_pack(agent, docs, packs=None):
    """组装注入包 = 路由文档（条目化，可单文件停用）+ 命名包（同）"""
    packs = packs or []
    doc_entries = norm_files(docs)
    head = (f"\n> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}｜组装者：{PLUGIN_ID}"
            f"｜文档：{', '.join(doc_label(e['path']) for e in doc_entries if e.get('enabled', True)) or '-'}")
    if packs:
        head += "｜命名包：" + "、".join(str(p.get("name") or p.get("id")) for p in packs)
    parts = [f"# 上下文包 · {agent}", head]
    for e in doc_entries:
        if not e.get("enabled", True):
            continue
        key = e["path"]
        content = read_doc(key)
        parts.append(f"\n## {doc_label(key)}")
        parts.append(content or f"(未找到文档：{key})")

    used, truncated = 0, False
    for p in packs:
        if truncated:
            break
        entries = norm_files(p.get("files"))
        parts.append(f"\n## 📎 {p.get('name') or p.get('id')}")
        if not entries:
            parts.append("(这个包还没挑文件)")
        for e in entries:
            rel = e["path"]
            if not e.get("enabled", True):
                parts.append(f"\n### {rel}\n(已停用，未纳入)")
                continue
            if used >= PACK_TOTAL_CHARS:
                parts.append("\n(包内容总量已达上限，其余文件未纳入)")
                truncated = True
                break
            text, err = read_vault_file(rel)
            parts.append(f"\n### {rel}")
            if err:
                parts.append(f"(读取失败：{err})")
                continue
            used += len(text)
            parts.append(text or "(空文件)")
    return "\n".join(parts)


def context_dir():
    """包目录：优先平台配置（/api/context/routes 响应不可知目录，用环境或默认）"""
    return os.environ.get("CONTEXT_DIR", os.path.join("data", "context"))


# ── 动作模型 + 分账（2026-09-11 新增）──────────────────────────
# 总账（权威）收状态行：发起 platform.action.started → 结束 platform.action.settled
# 分账（证据）收详情：写 data/ledger/detail/plugin.context-pack/*.jsonl，由状态行的 ref 指路
# 分账是旁路：任何失败只打印，绝不阻断插件主流程
_DETAIL = None
_last_details = []
# 点名要用的运行态（用 dict 避免 global 声明）
_probe_state = {"refreshing": False, "last_at": 0.0, "last_packs": 0}
_PROBE = None
# 组包串行锁：主循环（定时/事件）与 probe 线程（pack.* 变更）都会触发组包
_refresh_lock = threading.Lock()


def detail_ledger():
    """懒加载分账写入器（不可用时返回 None，不反复重试）"""
    global _DETAIL
    if _DETAIL is None:
        try:
            _DETAIL = DetailLedger(PLUGIN_ID)
            print(f"[ctx-pack] 分账已就绪 → {_DETAIL.dir}", flush=True)
        except Exception as e:
            print(f"[ctx-pack] 分账不可用（忽略，不影响主流程）: {e}", flush=True)
            _DETAIL = False
    return _DETAIL or None


def post_action(ev_type, action_id, name, extra=None, outcome=None, ref=None, expect_ms=None):
    """上报动作状态行（走 bridge；平台按 platform.* 前缀收进总账）"""
    action = {"id": action_id, "name": name}
    if expect_ms:
        action["expect_within_ms"] = expect_ms
    payload = {"action": action}
    if outcome:
        payload["outcome"] = outcome
    if ref:
        payload["ref"] = ref
    if extra:
        payload.update(extra)
    try:
        http_post("/api/bridge/send", {
            "id": f"{action_id}.{ev_type.rsplit('.', 1)[-1]}", "agentId": PLUGIN_ID, "kind": "event",
            "event": {"type": ev_type, "source": PLUGIN_ID, "payload": payload},
        })
    except Exception as e:
        print(f"[ctx-pack] 动作状态上报失败: {e}", flush=True)


def _start_action(reason):
    """动作发起（带 expect_within_ms：超期未结束即视为卡住）"""
    aid = f"act.ctxpack.{int(time.time()*1000)}"
    post_action("platform.action.started", aid, "context.pack.refresh",
                extra={"reason": reason}, expect_ms=20000)
    return aid


def _settle_action(action_id, reason, count, d, started):
    """动作结束：详情写分账 → 状态行带 outcome + ref 上报"""
    dur = int((time.time() - started) * 1000)
    _probe_state["refreshing"] = False
    _probe_state["last_at"] = time.time()
    _probe_state["last_packs"] = count
    details = list(_last_details)
    ref = None
    det = detail_ledger()
    if det is not None:
        try:
            refs = [det.append("context.pack.write", payload=item, action_id=action_id) for item in details]
            if refs:
                ref = dict(refs[0])
                ref["lines"] = len(refs)
                ref["bytes"] = sum(r.get("bytes", 0) for r in refs)
        except Exception as e:
            print(f"[ctx-pack] 分账写入失败（忽略）: {e}", flush=True)
    ok = bool(details) and all(item.get("ok") for item in details)
    post_action("platform.action.settled", action_id, "context.pack.refresh",
                extra={"reason": reason, "packs": count, "dir": d},
                outcome={"status": "ok" if ok else "error", "duration_ms": dur,
                         "code": "" if ok else "PACK_FAILED"},
                ref=ref)


def _probe_stats():
    """点名回执带的状态快照 → 平台 /api/status 活性快照"""
    last = _probe_state["last_at"]
    return {
        "packs": _probe_state["last_packs"],
        "named_packs": len(load_packs()),
        "refreshes": _stats["event_refresh"] + _stats["timer_refresh"],
        "last_refresh_ms_ago": int((time.time() - last) * 1000) if last else None,
        "watching": bool(HAS_WATCHDOG),
    }


def _probe_context_get(args):
    """点名顺手做件真实小事：回报各 agent 当前包状态（探活即探功能）"""
    d = context_dir()
    out = {}
    try:
        for name in os.listdir(d):
            if not name.endswith(".md"):
                continue
            path = os.path.join(d, name)
            out[name[:-3]] = {"bytes": os.path.getsize(path),
                              "mtime": time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(path)))}
    except Exception as e:
        out["error"] = str(e)
    return out


# ── 命名包工具（平台/控制台调用）─────────────────────────────
def _tool_pack_list(args):
    """列出命名包 + 每个 agent 的路由文档（控制台面板数据源）"""
    overrides = load_route_docs()
    names = load_folder_names()
    agents = []
    try:
        for r in http_get("/api/context/routes") or []:
            agent = r.get("agent")
            cfg_docs = norm_files(r.get("docs") or ["persona", "profile", "rules-l1"])
            used = overrides.get(agent)
            agents.append({
                "agent": agent,
                "enabled": bool(r.get("enabled")),
                "docs": used if used is not None else cfg_docs,
                "docs_overridden": used is not None,
                "docs_config": cfg_docs,
                "docs_missing": missing_files(used if used is not None else cfg_docs),
                "kind": "docs",                 # 统一卡片视图用（2026-09-13 加）
                "key": "docs:" + agent,
                # 默认包（原"默认文档清单"）统一成包形态，供前端一视同仁渲染
                "name": names.get(agent) or "默认包",
                "restore": True,
            })
    except Exception:
        pass
    packs = load_packs()
    for p in packs:
        p["files"] = norm_files(p.get("files"))
        p["files_missing"] = missing_files(p["files"])
        p["kind"] = "named"                    # 统一卡片视图用（2026-09-13 加）
        p["key"] = "named:" + str(p.get("id"))
        p["owner"] = str(p.get("owner") or "")  # 归属 agent 文件夹（空 = 通用）
    return {"packs": packs, "agents": agents, "path": packs_path(),
            "routes_docs_path": routes_docs_path(),
            "dir": context_dir(), "vault": VAULT,
            "max_files": MAX_PACK_FILES, "file_chars": PACK_FILE_CHARS}


def _tool_pack_create(args):
    """新建包：起名 + 挑文件（+ 可选指定 agent）"""
    name = str(args.get("name") or "").strip()
    if not name:
        raise ValueError("包名不能为空")
    files = args.get("files") or []
    if not isinstance(files, list):
        raise ValueError("files 必须是数组")
    if len(files) > MAX_PACK_FILES:
        raise ValueError(f"一个包最多 {MAX_PACK_FILES} 个文件（收到 {len(files)} 个）")
    ok_files, missing = validate_files(files)
    if missing:
        raise ValueError("这些条目不可用：" + missing_reason_text(missing))
    owner = str(args.get("owner") or "").strip()   # 归属的 agent 文件夹（2026-09-13 加）
    pack = {"id": f"pack-{int(time.time()*1000)}", "name": name, "files": ok_files,
            "agents": [str(a) for a in (args.get("agents") or [])],
            "owner": owner,
            "enabled": bool(args.get("enabled", True)),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    packs = load_packs()
    packs.append(pack)
    save_packs(packs)
    _after_pack_change("create", pack)
    return {"pack": pack, "total": len(packs)}


def _tool_pack_delete(args):
    """删除包（只删清单条目，不动 Vault 文件）"""
    pid = str(args.get("id") or "")
    packs = load_packs()
    doomed = next((p for p in packs if p.get("id") == pid), None)
    if doomed is None:
        raise ValueError(f"没有这个包：{pid}")
    save_packs([p for p in packs if p.get("id") != pid])
    # 留痕带上被删的整条记录：否则账本上只剩一个 id，"谁什么时候删了哪个包"就查不回来了
    _after_pack_change("delete", doomed)
    return {"deleted": pid, "total": len(packs) - 1}


def _tool_pack_update(args):
    """改包（名字/文件/目标 agent/开关；只改传进来的字段）"""
    pid = str(args.get("id") or "")
    packs = load_packs()
    target = next((p for p in packs if p.get("id") == pid), None)
    if target is None:
        raise ValueError(f"没有这个包：{pid}")
    if "name" in args:
        name = str(args.get("name") or "").strip()
        if not name:
            raise ValueError("包名不能为空")
        target["name"] = name
    if "files" in args:
        files = args.get("files") or []
        if len(files) > MAX_PACK_FILES:
            raise ValueError(f"一个包最多 {MAX_PACK_FILES} 个文件（收到 {len(files)} 个）")
        ok_files, missing = validate_files(files)
        if missing:
            raise ValueError("这些条目不可用：" + missing_reason_text(missing))
        target["files"] = ok_files
    if "agents" in args:
        target["agents"] = [str(a) for a in (args.get("agents") or [])]
    if "owner" in args:
        target["owner"] = str(args.get("owner") or "").strip()
    if "enabled" in args:
        target["enabled"] = bool(args.get("enabled"))
    save_packs(packs)
    _after_pack_change("update", target)
    return {"pack": target}


def _tool_pack_entries(args):
    """读/改某个 agent 的路由文档清单（关键字或文件路径，含单文件启停）。

    传 files 即视为改写（写入 routes-docs.json 覆盖 config 默认）；不传则只读。
    """
    agent = str(args.get("agent") or "").strip()
    if not agent:
        raise ValueError("agent 不能为空")
    overrides = load_route_docs()
    # 恢复默认：删掉该 agent 的覆盖（可选把默认快照写回去），下次组包用平台 config
    if args.get("reset"):
        defaults = load_route_defaults()
        if agent in overrides:
            del overrides[agent]
        snap = defaults.get(agent)
        if snap and args.get("keep_defaults", True):
            overrides[agent] = snap
        save_route_docs(overrides, defaults)
        _after_pack_change("entries-reset", {"id": agent, "name": "路由文档",
                                             "files": overrides.get(agent, []), "agents": [agent]})
        return {"agent": agent, "files": overrides.get(agent, []),
                "overridden": agent in overrides, "path": routes_docs_path(),
                "missing": missing_files(overrides.get(agent, []))}
    # 改名（默认包的显示名）：与 files 解耦——只传 name 也要能生效
    # 【踩坑】原先放在 files 分支末尾，只传 name 时会先在只读分支 return，改名永远执行不到
    if "name" in args:
        nm = str(args.get("name") or "").strip()
        if not nm:
            raise ValueError("包名不能为空")
        names = load_folder_names()
        names[agent] = nm
        save_folder_names(names)
        if "files" not in args:
            cur = overrides.get(agent)
            if cur is None:
                try:
                    for r in http_get("/api/context/routes") or []:
                        if r.get("agent") == agent:
                            cur = norm_files(r.get("docs") or [])
                            break
                except Exception:
                    cur = []
            return {"agent": agent, "files": cur or [], "overridden": agent in overrides,
                    "name": nm, "path": routes_docs_path(), "missing": missing_files(cur or [])}

    if "files" not in args:
        files = overrides.get(agent)
        if files is None:
            try:
                for r in http_get("/api/context/routes") or []:
                    if r.get("agent") == agent:
                        files = norm_files(r.get("docs") or [])
                        break
            except Exception:
                files = []
        return {"agent": agent, "files": files or [], "overridden": agent in overrides,
                "name": load_folder_names().get(agent) or "默认包",
                "path": routes_docs_path(), "missing": missing_files(files or [])}
    files = args.get("files") or []
    if len(files) > MAX_PACK_FILES:
        raise ValueError(f"路由文档最多 {MAX_PACK_FILES} 条（收到 {len(files)}）")
    entries = norm_files(files)
    bad = []
    for e in entries:
        if not e.get("enabled", True):
            continue
        if e["path"] in DOC_SOURCES:
            continue
        # 【2026-10-01 修老问题】原来只查磁盘（resolve_path），而命名包那条路走 entry_exists（库优先）
        # → 「只在库里、磁盘上没有」的条目在路由文档里被拒、在命名包里却收，同一个路径两套口径。
        # 外部绝对路径 entry_exists 同样认（它内部会落到 resolve_path）。
        okk, _err = entry_exists(e["path"])
        if not okk:
            bad.append(e["path"])
    if bad:
        raise ValueError("这些条目不可用：" + missing_reason_text(bad))
    overrides[agent] = entries
    defaults = load_route_defaults()
    if agent not in defaults:
        # 第一次覆盖：把当前平台 config 的 docs 记成"默认快照"（供恢复默认）
        try:
            for r in http_get("/api/context/routes") or []:
                if r.get("agent") == agent:
                    defaults[agent] = norm_files(r.get("docs") or [])
                    break
        except Exception:
            pass
    save_route_docs(overrides, defaults)
    _after_pack_change("entries", {"id": agent, "name": "路由文档", "files": entries, "agents": [agent]})
    return {"agent": agent, "files": entries, "overridden": True,
            "path": routes_docs_path(), "missing": missing_files(entries)}


def _tool_pack_set_file(args):
    """单文件级启停：把清单里某个条目的 enabled 翻成指定值（不动顺序、不删条目）。

    args: {kind: 'pack'|'docs', id?: packId, agent?: agentId, path, enabled}
    """
    kind = str(args.get("kind") or "pack")
    target = str(args.get("path") or "").strip()
    if not target:
        raise ValueError("path 不能为空")
    enabled = args.get("enabled", True) is not False
    if kind == "docs":
        agent = str(args.get("agent") or "").strip()
        if not agent:
            raise ValueError("kind=docs 时必须给 agent")
        overrides = load_route_docs()
        files = overrides.get(agent)
        if files is None:
            try:
                for r in http_get("/api/context/routes") or []:
                    if r.get("agent") == agent:
                        files = norm_files(r.get("docs") or [])
                        break
            except Exception:
                files = []
        files = norm_files(files or [])
        hit = False
        for e in files:
            if e["path"] == target:
                e["enabled"] = enabled
                hit = True
        if not hit:
            raise ValueError(f"该 agent 的路由文档里没有这个条目：{target}")
        overrides[agent] = files
        save_route_docs(overrides)
        _after_pack_change("set-file", {"id": agent, "name": "路由文档", "files": files, "agents": [agent]})
        return {"kind": "docs", "agent": agent, "path": target, "enabled": enabled, "files": files}
    pid = str(args.get("id") or "")
    packs = load_packs()
    pack = next((p for p in packs if p.get("id") == pid), None)
    if pack is None:
        raise ValueError(f"没有这个包：{pid}")
    files = norm_files(pack.get("files"))
    hit = False
    for e in files:
        if e["path"] == target:
            e["enabled"] = enabled
            hit = True
    if not hit:
        raise ValueError(f"该包里没有这个文件：{target}")
    pack["files"] = files
    save_packs(packs)
    _after_pack_change("set-file", pack)
    return {"kind": "pack", "id": pid, "path": target, "enabled": enabled, "files": files}


def _tool_pack_preview(args):
    """预览：看这些文件会往包里带什么（前 200 字符 + 字符数）

    **关键字要先过 DOC_SOURCES 映射**（2026-10-03 修）：原来把 `persona` / `profile` /
    `rules-l1` 当路径直接读，于是三个全报「文件不存在」（库里其实都有）—— 组装包那条路
    （`build_pack` → `read_doc`）是映射过的，预览这条漏了。
    对关键字还额外显示"**会进包的那一段**"（`rules-l1` 在包里本来就只取 1 级摘要），
    这样"看看内容"看到的就是包里真会出现的东西。
    """
    out = []
    for rel in (args.get("files") or [])[:MAX_PACK_FILES]:
        is_key = rel in DOC_SOURCES
        real = rel
        if is_key:
            real = norm_rel(DOC_SOURCES.get(rel, rel)) or DOC_SOURCES.get(rel, rel)
        text, err = read_vault_file(real)
        if is_key and not err:
            text = read_doc(rel)          # 与组装包一致（rules-l1 = 1 级摘要）
            if not text:
                err = "读取失败：库里和磁盘上都没有"
        item = {"path": rel, "ok": not err, "err": err,
                "chars": len(text), "head": text[:200]}
        if is_key:
            item["resolved"] = real
        out.append(item)
    return {"files": out}


def _after_pack_change(op, pack):
    """包清单变了的统一收尾：重建监听 → 立即重组包 → 记平台账"""
    # 注意（实测 2026-09-13）：重建监听时 watchdog 会把新目录里的现有文件当"新增"报一遍，
    # 于是紧接着还会触发一次事件驱动的重组（同内容写两遍，无害但不必要）。没去抑制它：
    # 抑制就得在重建后一段时间内忽略事件，而真编辑正好落在那段窗口就会被漏掉。
    try:
        restart_source_watchers()
    except Exception as e:
        print(f"[ctx-pack] 重建监听失败（忽略）: {e}", flush=True)
    try:
        refresh_once(reason=f"pack-{op}")
    except Exception as e:
        print(f"[ctx-pack] 变更后重组包失败: {e}", flush=True)
    try:
        http_post("/api/bridge/send", {
            "id": f"packchg-{int(time.time()*1000)}", "agentId": PLUGIN_ID, "kind": "event",
            "event": {"type": "platform.context.pack-changed", "source": PLUGIN_ID,
                      "payload": {"op": op, "pack_id": (pack or {}).get("id"),
                                  "name": (pack or {}).get("name"),
                                  "files": (pack or {}).get("files"),
                                  "agents": (pack or {}).get("agents")}},
        })
    except Exception as e:
        print(f"[ctx-pack] 包变更留痕失败: {e}", flush=True)


def start_probe():
    """启动心跳点名应答（平台按周期点名 → 回 pong/busy + stats）"""
    global _PROBE
    if _PROBE is not None:
        return _PROBE
    _PROBE = Probe(PLUGIN_ID,
                   handlers={"context.get": _probe_context_get,
                             "pack.list": _tool_pack_list,
                             "pack.create": _tool_pack_create,
                             "pack.delete": _tool_pack_delete,
                             "pack.update": _tool_pack_update,
                             "pack.preview": _tool_pack_preview,
                             "pack.entries": _tool_pack_entries,
                             "pack.set-file": _tool_pack_set_file},
                   stats_provider=_probe_stats,
                   busy_provider=lambda: _probe_state["refreshing"])
    _PROBE.start()
    return _PROBE


def refresh_once(reason="timer"):
    """组包入口（串行）：主循环与 probe 线程都走这里"""
    with _refresh_lock:
        return _refresh_locked(reason)


def _refresh_locked(reason="timer"):
    """取路由 → 为每个 enabled 的 agent 组装并写包（路由默认 docs + 命名包）"""
    started = time.time()
    action_id = _start_action(reason)
    _last_details.clear()
    _probe_state["refreshing"] = True
    try:
        routes = http_get("/api/context/routes")
    except Exception as e:
        print(f"[ctx-pack] 取路由失败（平台未就绪？）: {e}", flush=True)
        return 0
    d = context_dir()
    os.makedirs(d, exist_ok=True)
    packs = load_packs()
    overrides = load_route_docs()
    count = 0
    for r in routes or []:
        agent = r.get("agent", "")
        if not agent or not r.get("enabled"):
            continue
        cfg_docs = r.get("docs") or ["persona", "profile", "rules-l1"]
        docs = overrides.get(agent)
        if docs is None:
            docs = norm_files(cfg_docs)
        my_packs = packs_for_agent(agent, packs)
        pack = build_pack(agent, docs, my_packs)
        path = os.path.join(d, agent + ".md")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(pack)
            count += 1
            _last_details.append({"agent": agent, "path": path, "chars": len(pack),
                                  "docs": paths_of(docs),
                                  "named": [p.get("id") for p in my_packs],
                                  "ok": True})
            print(f"[ctx-pack] 已生成 {agent} 的包（{len(pack)} 字符 / 命名包 "
                  f"{len(my_packs)} 个 → {path}）[{reason}]", flush=True)
        except Exception as e:
            _last_details.append({"agent": agent, "path": path, "ok": False, "err": str(e)})
            print(f"[ctx-pack] 写包失败 {agent}: {e}", flush=True)
    _settle_action(action_id, reason, count, d, started)

    if reason == "event":
        _stats["event_refresh"] += 1
    elif reason.startswith("pack-"):
        pass  # 包变更不计入两条常规刷新计数（它有自己的账）
    else:
        _stats["timer_refresh"] += 1
    # 通知平台（事件留痕 + 拓扑可见）
    try:
        http_post("/api/bridge/send", {
            "id": f"pack-{int(time.time()*1000)}", "agentId": PLUGIN_ID, "kind": "event",
            "event": {"type": "platform.context.pack-ready", "source": PLUGIN_ID,
                      "payload": {"packs": count, "dir": d, "reason": reason,
                                  "named": sum(len(packs_for_agent(r.get("agent", ""), packs))
                                               for r in (routes or []) if r.get("enabled"))}},
        })
    except Exception:
        pass
    return count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default=PLUGIN_ID)
    ap.add_argument("--interval", type=float,
                    default=float(os.environ.get("PACK_INTERVAL", 300)))
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    # 注册身份（幂等）
    try:
        http_post("/api/bridge/plugins/register", {
            "id": args.id, "name": "上下文包",
            "description": "把 Vault 里的人设 / 用户画像 / 一级规则，加上控制台新建的「命名包」"
                           "（可挑任意笔记文件），按路由组装成「注入包」，写 data/context/<agent>.md；"
                           "会话开始时 agent 侧取走并注入模型上下文，源文件一改就重新组包"
                           "（事件触发 + 定时兜底 + 包变更）。",
            "capabilities": ["context.pack", "pack.list", "pack.create",
                             "pack.delete", "pack.update", "pack.preview",
                             "pack.entries", "pack.set-file"],
            "produces": ["platform.context.pack-ready", "platform.context.pack-changed"],
            "consumes": [],
        })
        print(f"[ctx-pack] 注册成功 {args.id}", flush=True)
    except Exception as e:
        print(f"[ctx-pack] 注册失败（平台未就绪）: {e}", flush=True)

    # 心跳点名应答（平台按周期点名）
    start_probe()

    # 首轮组包 + 启动源文件监听
    refresh_once(reason="startup")
    restart_source_watchers()

    next_at = time.time() + args.interval
    next_parent_check = time.time() + PARENT_CHECK_SECONDS
    while running:
        time.sleep(0.5)
        if not running:
            break
        now = time.time()
        # 父进程（core）没了就跟着退出，避免变成孤儿继续抢着组包
        if now >= next_parent_check:
            next_parent_check = now + PARENT_CHECK_SECONDS
            if not _pid_alive(_PARENT_PID):
                print("[ctx-pack] 父进程(core)已退出 → 自行退出，避免孤儿", flush=True)
                break
        # ① 事件驱动：源文件变化（防抖合并后）立即重组
        if _pending["dirty"] and now >= _pending["due"]:
            _pending["dirty"] = False
            print("[ctx-pack] 检测到源文件变化 → 事件触发重组包", flush=True)
            refresh_once(reason="event")
            next_at = now + args.interval
        # ② 定时兜底
        elif now >= next_at:
            refresh_once(reason="timer")
            next_at = now + args.interval

    print(f"[ctx-pack] 正常退出（事件触发 {_stats['event_refresh']} 次 / "
          f"定时刷新 {_stats['timer_refresh']} 次）", flush=True)
    _det = detail_ledger()
    if _det is not None:
        _det.close()
    if _PROBE is not None:
        _PROBE.stop()
    if _OBSERVER.get("obs") is not None:
        try:
            _OBSERVER["obs"].stop()
        except Exception:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()


