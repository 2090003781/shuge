#!/usr/bin/env python3
"""投递箱（独立进程插件）：盯一个目录，新增/变化的文件自动索引进库 —— "拖入即出现"。

辞玖 2026-09-21（文档线第 2 步）定的事，2026-09-22 升级为**事件驱动 + 兜底对账**：

    ┌ 事件驱动（快）  Windows 的 FindFirstChangeNotification 盯整棵子树（一个句柄），
    │                 有变化就唤醒 → 去抖 → 跑一轮扫描（全树 57ms，便宜）
    ├ 兜底对账（不漏）  每 --interval（默认 60 秒）无条件扫一轮：补"事件丢了/进程宕过/网络盘不发事件"
    └ 只进不出（v1 边界）改名/删除不跟随；同名不同内容不覆盖（记 changed_skipped）

为什么不是"纯事件驱动"（三条硬理由，都是踩过或已知的）：
  ① 事件会丢：ReadDirectoryChangesW/FindFirstChangeNotification 的通知是**合并+可溢出**的，
     大量文件一次涌入时可能只给一个甚至丢通知；进程宕机期间的变化更没有事件；
     网络盘/虚拟机共享目录常常干脆不发事件 —— 所以**必须**有一条周期扫描兜底。
  ② 事件在"第一次写"时就来，不是"写完了"：直接读会读到半个文件 → 仍要 --settle 安静阈值。
  ③ 通知只说"这棵树变了"，不说"哪个文件怎么变"（粗通知）：反正全扫只要几十毫秒，扫一遍最稳。

为什么放平台侧而不是库进程里：库是单线程服务、sqlite 连接不能跨线程；平台插件有监督/重启、
控制台组件列表与日志里看得见；一次导入就是一次 HTTP 调用，天然可重试。
"""
import argparse
import ctypes
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))

from probe import Probe, auth_headers  # noqa: E402

CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:8790")
CORE_DIR = os.environ.get("CORE_DIR", os.path.dirname(HERE))
LIB_URL = os.environ.get("AINOTES_URL", "http://127.0.0.1:8848")
PLUGIN_ID = "plugin.inbox"

# 可进库的扩展名：与库的导入器保持一致（文本类 + 文档类 + 表格原料）
TEXT_EXTS = {".md", ".markdown", ".txt", ".log", ".py", ".js", ".ts", ".go", ".java", ".c", ".cpp",
             ".h", ".rs", ".rb", ".php", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sql",
             ".sh", ".ps1", ".css", ".html"}
DOC_EXTS = {".docx", ".docm", ".dotx", ".odt", ".ott", ".rtf", ".pdf", ".epub"}
TABLE_EXTS = {".csv", ".tsv", ".xlsx", ".xlsm"}
IMPORT_EXTS = TEXT_EXTS | DOC_EXTS | TABLE_EXTS
EXCLUDE_DIRS = {".git", ".trash", ".obsidian", "node_modules", "__pycache__", "tmp",
                ".smart-env", ".claudian", ".dsh"}
EXCLUDE_PREFIXES = ["日志/reasonix-raw"]              # 纯中间产物才排除
PRIVATE_PREFIXES = ["个人/私密"]                        # 照进库，但按 private 上权限（--private-prefix 可改）

# FindFirstChangeNotification 的过滤器：文件名 / 目录名 / 大小 / 最后写入
FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
FILE_NOTIFY_CHANGE_SIZE = 0x00000008
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
WAIT_OBJECT_0 = 0x00000000
FS_FILTER = (FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_DIR_NAME
             | FILE_NOTIFY_CHANGE_SIZE | FILE_NOTIFY_CHANGE_LAST_WRITE)

running = True
_locked_notice = {"at": 0.0, "n": -1}      # "在等解锁"的提醒节流（数量变了立刻说，否则 5 分钟一次）
_stats = {"scanned": 0, "imported": 0, "skipped": 0, "changed_skipped": 0, "failed": 0,
          "registered": 0, "errors": 0, "scans": 0, "event_scans": 0, "timer_scans": 0,
          "events": 0, "deferred": 0, "followup_scans": 0, "watch_mode": "",
          "updated": 0, "kept": 0, "secured": 0, "private_imported": 0, "locked_wait": 0,
          "private_stuck": 0, "refs_converted": 0,
          "mirror_runs": 0, "mirror_in": 0, "mirror_out": 0, "mirror_errors": 0,
          "last_at": 0.0, "last_file": "", "watch": "",
          "first_run_done": False}


def handle_stop(signum, frame):
    global running
    print(f"[inbox] 收到信号 {signum}，优雅退出", flush=True)
    running = False


def _pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def http_post(url, payload, timeout=300, auth=False):
    """默认不带鉴权（库是本机服务）；注册那种插件面调用要带 token，否则平台回 401。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    base = auth_headers({"Content-Type": "application/json"}) if auth else {"Content-Type": "application/json"}
    # 自报家门：库把这记进 events.writer（辞玖 2026-09-24 定的写入者标签）
    base["X-Writer"] = "inbox"
    headers = base
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def norm(p):
    return p.replace("\\", "/")


# ── 事件驱动：粗通知盯整棵子树 ────────────────────────────────
def start_fs_watch(path: str, wake: threading.Event, stats: dict):
    """Windows：FindFirstChangeNotification(bWatchSubtree=True)。

    返回 (句柄, 线程) 或 (None, None)。**只当"该扫描了"的信号**用 ——
    通知是合并的、可能丢的、也不给明细，所以收到就全扫一轮（57ms，便宜）。
    """
    if os.name != "nt":
        return None, None
    try:
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.FindFirstChangeNotificationW.restype = wintypes.HANDLE
        k32.FindFirstChangeNotificationW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD]
        k32.FindNextChangeNotification.argtypes = [wintypes.HANDLE]
        k32.FindCloseChangeNotification.argtypes = [wintypes.HANDLE]
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        h = k32.FindFirstChangeNotificationW(path, True, FS_FILTER)
        invalid = wintypes.HANDLE(-1).value
        if not h or h == invalid:
            print(f"[inbox] 事件监听建不起来（err={ctypes.get_last_error()}）→ 退回纯轮询", flush=True)
            return None, None

        def loop():
            while running:
                r = k32.WaitForSingleObject(h, 500)      # 500ms 一跳，好让退出及时
                if not running:
                    break
                if r == WAIT_OBJECT_0:
                    stats["events"] += 1
                    wake.set()
                    if not k32.FindNextChangeNotification(h):
                        print("[inbox] FindNextChangeNotification 失败 → 事件监听停止（对账扫描仍在跑）",
                              flush=True)
                        break
            try:
                k32.FindCloseChangeNotification(h)
            except Exception:
                pass

        t = threading.Thread(target=loop, daemon=True, name="inbox-fswatch")
        t.start()
        return h, t
    except Exception as e:
        print(f"[inbox] 事件监听异常（{type(e).__name__}: {e}）→ 退回纯轮询", flush=True)
        return None, None


# ── 扫描与导入 ───────────────────────────────────────────────
def accepted(rel: str, name: str) -> bool:
    if name.startswith("."):
        return False
    if os.path.splitext(name)[1].lower() not in IMPORT_EXTS:
        return False
    for pre in EXCLUDE_PREFIXES:
        if norm(rel).startswith(pre):
            return False
    return True


def is_private(rel: str, prefixes) -> bool:
    """这个相对路径算不算私密（整段对齐前缀，免得 个人/私密X 被误判）"""
    r = norm(rel)
    return any(r == p or r.startswith(p.rstrip("/") + "/") for p in prefixes)


def scan(root: str):
    """遍历被盯目录，产出 (绝对路径, 相对目录, 文件名, mtime, size)"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for fn in sorted(filenames):
            ap = os.path.join(dirpath, fn)
            rel = os.path.relpath(ap, root)
            if not accepted(rel, fn):
                continue
            try:
                stt = os.stat(ap)
            except OSError:
                continue
            yield ap, norm(os.path.dirname(rel)), fn, stt.st_mtime, stt.st_size


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None            # None = 首次启动（没有状态文件）


def save_state(path, st):
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[inbox] 状态落盘失败（不影响导入）: {e}", flush=True)


def run_scan(root, under, state, *, first_run, import_existing, rag, settle, overwrite=True,
             private_prefixes=(), do_mirror=True):
    """一轮扫描+导入。返回**本轮因"还在写"而推迟的文件数** —— 调用方据此安排一次补扫。

    为什么必须返回它（2026-09-22 实测踩到）：事件通知只来一次；文件刚落地时太新被 settle 挡下，
    若没人再触发，它就得等到下一次对账（60 秒）才进库 —— 拖一个文件要等一分钟，等于没做事件驱动。
    """
    now = time.time()
    seen = set()
    deferred = 0
    locked = []                     # 因库未解锁而没进去的私密文件

    # ① 先让库把两个文件夹对齐（库文件夹 ⇄ vault），并把它那边的改动导进库 —— 非私密的事全归它。
    #    一轮镜像约 3.5 秒（库单线程，跑的时候接口排队）：**事件触发必跑；纯对账那拍每 5 拍兜一次**，
    #    稳态下别每分钟白占 3.5 秒，又不能完全不兜（漏事件时靠它找回来）。
    if do_mirror:
        try:
            d = http_post(LIB_URL + "/api/mirror/sync", {"apply": True})
            m = d.get("mirror") or {}
            imp = d.get("import") or {}
            _stats["mirror_runs"] += 1
            _stats["mirror_in"] += len(m.get("copied_to_library") or [])
            _stats["mirror_out"] += len(m.get("copied_to_vault") or [])
            _stats["imported"] += int(imp.get("imported") or 0)
            _stats["updated"] += int(imp.get("updated") or 0)
            _stats["secured"] += int(imp.get("secured") or 0)
            _stats["refs_converted"] += int(imp.get("refs_converted") or 0)
            bad = (m.get("errors") or []) + (imp.get("errors") or [])
            if bad:
                _stats["mirror_errors"] += len(bad)
                print("[inbox] 镜像有问题：%s" % bad[:2], flush=True)
            if m.get("copied_to_library") or m.get("copied_to_vault"):
                print("[inbox] 镜像：vault→库 %d 篇、库→vault %d 篇、入册 %d 篇"
                      % (len(m.get("copied_to_library") or []), len(m.get("copied_to_vault") or []),
                         int(imp.get("imported") or 0) + int(imp.get("updated") or 0)), flush=True)
        except Exception as e:
            _stats["mirror_errors"] += 1
            print("[inbox] 镜像同步失败（下一轮再试）：%s" % e, flush=True)
    stuck = []                      # 私密没进到位（库里可能还是明文）→ 不记账，下轮再试
    for ap_, rel_dir, fn, mtime, size in scan(root):
        key = norm(os.path.relpath(ap_, root))
        seen.add(key)
        _stats["scanned"] += 1
        prev = state.get(key)
        if prev and abs(prev.get("mtime", 0) - mtime) < 0.001 and prev.get("size") == size:
            continue                                   # 没变
        if now - settle < mtime <= now + 5:
            deferred += 1                              # 还在写：本轮先不动它（补扫会再来）
            continue                                   # mtime 在未来 >5s 的当"已写完"，不然它永远进不来
        priv = is_private(key, private_prefixes)     # 相对路径在私密前缀下 → 进库但上权限
        if not priv:
            continue                                  # 非私密：上面那次镜像已经管了
        if first_run and not import_existing and prev is None:
            state[key] = {"mtime": mtime, "size": size, "at": time.time()}
            _stats["registered"] += 1
            continue
        # 注意：**记账放在成功之后**（原来在导入前就记，失败会被当成"已处理"，永远不重试）
        sub = "/".join(x for x in (under.strip("/"), rel_dir) if x and x != ".")
        try:
            body = {"src": ap_, "under": sub, "rag": bool(rag)}
            if overwrite:
                body["overwrite"] = True
            if priv:
                body["private"] = True                   # 私密进库：库侧加密 + visibility=private
            try:
                d = http_post(LIB_URL + "/api/import", body)
            except urllib.error.HTTPError as e:
                if priv and e.code == 403:
                    # 库没解锁 → 没法加密 → 不导、也不记账（解锁后自己补上）
                    _stats["locked_wait"] += 1
                    locked.append(key)
                    continue
                raise
            got = int(d.get("imported") or 0)
            skip = int(d.get("skipped") or 0)
            upd = int(d.get("updated") or 0)
            keep_win = int(d.get("kept") or 0)
            sec = int(d.get("secured") or 0)         # 库里已有这篇、这次只把权限补成私密
            ready = int(d.get("private_ready") or 0)  # 库里那份已经是私密（这次不用动手）
            refs = int(d.get("refs_converted") or 0)  # 进库时按库规则转掉的 [[目标]] 处数
            _stats["refs_converted"] += refs
            _stats["imported"] += got
            _stats["skipped"] += skip
            _stats["updated"] += upd
            _stats["kept"] += keep_win
            _stats["secured"] += sec
            if priv:
                _stats["private_imported"] += got + upd + sec
            if priv and (got + upd + sec + ready) == 0:
                # 私密这件事**没成立**（不是"库里已经是私密"）→ 别记账，下一轮再来
                _stats["private_stuck"] += 1
                stuck.append(key)
                continue
            state[key] = {"mtime": mtime, "size": size, "at": time.time(), "private": priv}
            if keep_win:
                # 库里那份比磁盘新 → 没动它。必须说出来，否则就是"悄悄没跟"
                print(f"[inbox] 库内版本更新，磁盘这份没覆盖（让库赢）：{key}", flush=True)
            elif priv:
                print(f"[inbox] 私密进库 {key} → （{sub or '/'}）private=1 "
                      f"imported={got} updated={upd} 补权限={sec}", flush=True)
            elif upd:
                rz = d.get("reindexed") or {}
                print(f"[inbox] 跟新 {key} → （{sub or '/'}）updated=1 chunks={rz.get('chunks')}", flush=True)
                if rz.get("left"):
                    print(f"[inbox] 注意：本轮还有 {rz['left']} 篇覆盖没跟进派生层（下次重扫补）",
                          flush=True)
            elif got == 0 and skip and prev is not None:
                _stats["changed_skipped"] += 1
                print(f"[inbox] 内容变了但库里同名已存在（第一期不覆盖）：{key}", flush=True)
            else:
                tail = f" 转引用={refs}" if refs else ""
                print(f"[inbox] 进库 {key} → （{sub or '/'}）imported={got} skipped={skip}{tail}",
                      flush=True)
            _stats["last_file"] = key
            _stats["last_at"] = time.time()
        except Exception as e:
            _stats["failed"] += 1
            print(f"[inbox] 导入失败（下轮还会再试） {key}: {e}", flush=True)
    # 剪枝：磁盘上已经没有的，从状态里去掉（否则"删了又放回来"会被误判没变）
    for gone in [k for k in list(state.keys()) if k not in seen]:
        state.pop(gone, None)
    if (locked or stuck) and (len(locked) + len(stuck) != _locked_notice["n"]
                              or time.time() - _locked_notice["at"] > 300):
        # 私密内容没进库这件事必须露出来，不能静默；但同一条别每轮都刷（数量变了立刻说）
        _locked_notice["at"] = time.time()
        _locked_notice["n"] = len(locked) + len(stuck)
        tip = []
        if locked:
            tip.append(f"{len(locked)} 个私密文件在等库解锁（解锁后自动补，不会丢）："
                       f"{'、'.join(locked[:3])}{' …' if len(locked) > 3 else ''}")
        if stuck:
            tip.append(f"{len(stuck)} 个私密没进到位（下轮再试）："
                       f"{'、'.join(stuck[:3])}{' …' if len(stuck) > 3 else ''}")
        print("[inbox] " + "；".join(tip), flush=True)
    _stats["scans"] += 1
    _stats["errors"] = _stats["failed"]
    return deferred


def status(_args=None):
    return dict(_stats)


def main():
    global running
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default=PLUGIN_ID)
    ap.add_argument("--watch", default=os.environ.get("VAULT_ROOT", ""), help="被盯的目录（投递箱）")
    ap.add_argument("--under", default="", help="落到库内哪个目录下（默认按相对路径镜像到根）")
    ap.add_argument("--private-prefix", action="append", metavar="相对路径", default=None,
                    help="这些前缀下的文件照样进库，但按 private 上权限（可多次；默认 个人/私密）")
    ap.add_argument("--no-overwrite", action="store_true",
                    help="回到 v1：同名不同内容一律跳过（默认是跟新：磁盘新的覆盖库里的）")
    ap.add_argument("--interval", type=float, default=60.0, help="**兜底对账**间隔秒（默认 60；事件仍然即时触发）")
    ap.add_argument("--settle", type=float, default=1.0,
                    help="文件安静这么久才处理（防半个文件；被挡下的会安排一次补扫）")
    ap.add_argument("--debounce", type=float, default=0.5, help="收到事件后先等这么久再扫（把连写合并）")
    ap.add_argument("--no-events", action="store_true", help="关掉事件监听，退回纯轮询（逃生门）")
    ap.add_argument("--state", default=os.path.join(CORE_DIR, "data", "inbox-state.json"))
    ap.add_argument("--import-existing", action="store_true",
                    help="首次启动就把存量文件全导一遍（默认只记账、不动存量）")
    ap.add_argument("--rag", action="store_true", help="导入时一并加入检索库（默认只存放）")
    a = ap.parse_args()

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    root = os.path.abspath(a.watch) if a.watch else ""
    _stats["watch"] = root
    if not root or not os.path.isdir(root):
        print(f"[inbox] 被盯目录不存在：{root or '(空)'} —— 插件空转（改 config.yaml 的 --watch）", flush=True)
    else:
        print(f"[inbox] 投递箱：{root} → 库 {LIB_URL}（under={a.under or '/'}，"
              f"rag={'开' if a.rag else '关（默认只存放）'}）", flush=True)

    try:
        http_post(CORE_URL + "/api/bridge/plugins/register", {
            "id": a.id, "name": "投递箱",
            "description": "盯一个磁盘目录：新增/变化的文件自动索引进库（拖入即出现）。"
                           "事件驱动（FindFirstChangeNotification 盯整棵子树）+ 兜底对账扫描；"
                           "默认只存放、不进检索；改名/删除不跟随（v1 边界）。状态工具 inbox.status。",
            "capabilities": ["inbox.watch"], "produces": [], "consumes": [],
        }, auth=True)
        print(f"[inbox] 注册成功 {a.id}", flush=True)
    except Exception as e:
        print(f"[inbox] 注册失败（平台未就绪）: {e}", flush=True)

    probe = Probe(a.id, handlers={"inbox.status": status}, stats_provider=lambda: dict(_stats), core_url=CORE_URL)
    probe.start()

    state = load_state(a.state)
    first_run = state is None
    state = state or {}
    if first_run:
        print("[inbox] 首次启动：先记账不动存量（要全量镜像请用 --import-existing 重启本插件）", flush=True)

    wake = threading.Event()
    handle, _t = (None, None) if (a.no_events or not root) else start_fs_watch(root, wake, _stats)
    if not root:
        _stats["watch_mode"] = "empty"
    elif handle:
        _stats["watch_mode"] = "events+timer"
        print(f"[inbox] 事件驱动已启用（FindFirstChangeNotification 盯整棵子树）"
              f"；兜底对账每 {a.interval:g} 秒一轮；收到事件后等 {a.debounce:g} 秒再扫", flush=True)
    else:
        _stats["watch_mode"] = "timer(%gs)" % a.interval
        print(f"[inbox] 纯轮询模式：每 {a.interval:g} 秒扫一轮"
              f"{'（--no-events）' if a.no_events else '（事件监听不可用）'}", flush=True)

    parent_pid = os.getppid()
    next_parent = time.time() + 5
    private_prefixes = list(a.private_prefix) if a.private_prefix is not None else list(PRIVATE_PREFIXES)
    if private_prefixes:
        print(f"[inbox] 私密进库（照进，按 private 上权限）：{'、'.join(private_prefixes)}", flush=True)
    next_timer = time.time() + a.interval
    followup_streak = 0
    while running:
        time.sleep(0.2)
        now = time.time()
        if now >= next_parent:
            next_parent = now + 5
            if not _pid_alive(parent_pid):
                print("[inbox] 父进程(core)已退出 → 自行退出，避免孤儿", flush=True)
                break
        if not root:
            continue
        by_event = wake.is_set()
        if not by_event and now < next_timer:
            continue
        if by_event:
            wake.clear()
            time.sleep(a.debounce)        # 把"边写边触发"的连写合并掉
            wake.clear()                  # 期间再来事件也不用管——马上要全扫
            _stats["event_scans"] += 1
        else:
            _stats["timer_scans"] += 1
        # 事件触发 → 跑镜像；纯对账那拍每 5 拍（约 5 分钟）兜一次
        do_mirror = bool(by_event) or first_run or (_stats["timer_scans"] % 5 == 1)
        try:
            deferred = run_scan(root, a.under, state, first_run=first_run,
                                import_existing=a.import_existing, rag=a.rag, settle=a.settle,
                                overwrite=not a.no_overwrite,
                                private_prefixes=tuple(private_prefixes),
                                do_mirror=do_mirror)
            save_state(a.state, state)
            first_run = False
            _stats["first_run_done"] = True
            _stats["deferred"] = deferred
            # 有"还在写"的文件 → 安排一次补扫（settle 秒后再扫），别等到下一次对账；
            # 连补 3 轮还没干净就退回对账节奏，免得异常文件把补扫变成热轮询
            if deferred and followup_streak < 3:
                followup_streak += 1
                next_timer = time.time() + max(0.5, a.settle) + 0.2
                _stats["followup_scans"] += 1
            else:
                followup_streak = 0
                next_timer = time.time() + a.interval
        except Exception as e:
            _stats["errors"] += 1
            print(f"[inbox] 扫描出错: {e}", flush=True)
            next_timer = time.time() + a.interval

    try:
        probe.stop()
    except Exception:
        pass
    print(f"[inbox] 正常退出（扫描 {_stats['scans']} 轮：事件触发 {_stats['event_scans']} + 对账 "
          f"{_stats['timer_scans']}）", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
