#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""MCP 面的「多格式」统一处理：格式 × 形状 的识别 → 读 → 写 → 校验。

缘起（2026-10-05 晚，辞玖：「配置文件考虑一下 toml、config、yaml 等多格式问题…在别人电脑上不会有信给你」）：
不同宿主读的配置**格式与形状都不一样**，写坏一份就把它已有的服务全冲掉。已知的真实形状：

| 格式 | 形状 | 谁是这样 |
|---|---|---|
| JSON | `{"mcpServers": {...}}` | Claude Desktop / Cursor / WorkBuddy / Cline |
| TOML | `[mcp_servers.<名字>]` | Codex CLI |
| TOML | `[[plugins]]`（External MCP servers） | Reasonix |
| YAML | `mcpServers:` / `mcp:` / `context_servers:` | DSH(cordis) / Zed / Continue |
| 命令 | 走它自己的 CLI 注册 | Claude Code、Reasonix(有 cli) |

对外两件套：`face_of()` 判"这一面是什么格式 x 形状、我们写不写"，`snippet()` 出能直接抄的片段；
`write_entry()` 负责落盘（备份 -> 写 -> 回读校验 -> 不过就回滚）。

**写**这件事各家差别更大，所以这一层的规矩是：
  · JSON 只能整份往返（JSON 本来没注释，安全）；
  · TOML 优先用 `tomlkit`（**保留注释**），没装才退回"文本追加"；
  · YAML **没有**保注释的写入器 → 一律**文本插入**（只动目标那一块，别处一个字节不改）；
  · 不管哪种：写前备份、写完**立刻用对应解析器回读校验**，解析不过就**回滚**（坏文件绝不留下）。
"""
import io
import json
import os
import re
import shutil

try:
    import tomllib                     # py3.11 内置：读 TOML
except Exception:
    tomllib = None
try:
    import tomlkit                     # 能保留注释地写 TOML
except Exception:
    tomlkit = None
try:
    import yaml
except Exception:
    yaml = None

FORMATS = ("json", "toml", "yaml", "cli", "none")
SHAPES = ("mcpServers", "mcp_servers", "context_servers", "plugins_list", "mcp_key", "cli", "none")

# kind（老字段）→ (格式, 形状, 我们写不写)
KIND_MAP = {
    "mcp-json": ("json", "mcpServers", True),
    "codex-toml": ("toml", "mcp_servers", True),
    "reasonix-toml": ("toml", "plugins_list", False),      # 只读：它有自己的 CLI
    "dsh-plugin": ("yaml", "plugins_list", True),          # cordis 的 - id: 列表（形状特殊，见 write 里的插件分支）
    "claude-cli": ("cli", "cli", True),
    "none": ("none", "none", False),
    "": ("none", "none", False),
}

# 各形状在 YAML 里的顶层键名（文本插入用）
YAML_TOPKEYS = {"mcpServers": "mcpServers", "mcp_servers": "mcp_servers",
                "context_servers": "context_servers", "mcp_key": "mcp"}
TOML_TABLES = {"mcp_servers": "mcp_servers", "mcpServers": "mcp_servers",
               "context_servers": "context_servers", "plugins_list": "plugins"}


def key_for(fmt, shape, table=None):
    r"""表名要**按格式**取：JSON/YAML 看顶层键（mcpServers / context_servers），
    TOML 看表名（mcp_servers / plugins）。

    2026-10-05 踩过：一律用 YAML 那套回退 → 给 [[plugins]] 写成了 [[plugins_list]]（自己造的表名），
    而且因为回读也认不出来，第二次写又追加一遍。"""
    if table:
        return table
    if fmt == "toml":
        return TOML_TABLES.get(shape) or shape
    return YAML_TOPKEYS.get(shape) or shape


def fs_of(kind):
    """老 kind → (format, shape, writable)"""
    return KIND_MAP.get((kind or "").lower(), ("none", "none", False))


def face_of(kind="", fmt="", shape="", path="", readonly=None):
    """把一个「面」定成（格式, 形状, 我们写不写, 依据）。

    三种来源，优先级从高到低 —— 这也是给外面（agentctl）用的唯一入口：
      ① 面里**显式**写了 `format:` / `shape:`（新写法，最准，没见过的新宿主也能接）；
      ② 老字段 `kind:`（mcp-json / codex-toml / …）查表（老描述文件不用改）；
      ③ 两个都没给 -> **看文件自己认**（按扩展名猜格式 + 探有没有 MCP 表）；认不出就 none，**不硬猜**。
    """
    kind = (kind or "").lower()
    fmt = (fmt or "").lower()
    shape = shape or ""
    explicit = bool(fmt or shape)
    # ⚠️ `""` 和 `"none"` 都在 KIND_MAP 里：空 kind = "没给"（要看文件自己认），
    #    `none` = "作者说它不吃 MCP" —— 但那时**如果显式给了 format/shape，还是要按显式的来**
    #    （2026-10-05 模拟器第三轮：mcp_hosts 把没写 kind 的规范成 none，于是显式的面被判成只读）。
    known = bool(kind) and kind != "none" and kind in KIND_MAP
    if not explicit and known:
        kf, ks, kw = KIND_MAP[kind]
        return kf, ks, (False if readonly else kw), "kind"
    if not explicit:
        hits = detect_shapes(path)[1] if (path and os.path.isfile(path)) else []
        g = guess_format(path, read_text(path)) if path else "unknown"
        g = g if g in ("json", "toml", "yaml") else "none"
        return g, (hits[0][0] if hits else "none"), bool(hits), ("detected" if hits else "unknown")
    kf, ks, kw = fs_of(kind) if known else ("none", "none", False)   # 显式给了：缺的那半看 kind 表
    fmt = fmt or kf
    shape = shape or ks
    if not shape and fmt in ("json", "toml", "yaml"):
        hits = detect_shapes(path)[1] if (path and os.path.isfile(path)) else []
        shape = hits[0][0] if hits else "none"
    shape = shape or "none"
    writable = (kw if known else (fmt in ("json", "toml", "yaml"))) and shape != "none"
    return (fmt or "none"), shape, (False if readonly else writable), "explicit"


def snippet(fmt, shape, name, command, args, cwd, table=None):
    """按（格式 x 形状）出一个**能直接抄**的片段（给人/给 agent 看的那种）。

    跟 write_entry 同源 —— 片段里写成什么样，落盘就写成什么样（不然又是"照抄了却不通"）。
    """
    key = key_for(fmt, shape, table)
    cmd = command or ""
    argl = [str(a) for a in (args or [])]
    if fmt == "json":
        return json.dumps({key: {name: {"command": cmd, "args": argl, "cwd": cwd}}},
                          ensure_ascii=False, indent=2)
    if fmt == "toml":
        arr = "[" + ", ".join('"%s"' % a for a in argl) + "]"
        if shape == "plugins_list":
            return ('[[%s]]\nname = "%s"\ncommand = "%s"\nargs = %s\ncwd = "%s"'
                    % (key, name, cmd.replace("\\", "\\\\"), arr,
                       str(cwd).replace("\\", "\\\\")))
        return ('[%s.%s]\ncommand = "%s"\nargs = %s\ncwd = "%s"'
                % (key, name, cmd.replace("\\", "\\\\"), arr,
                   str(cwd).replace("\\", "\\\\")))
    if fmt == "yaml":
        return ('%s:\n  %s:\n    command: "%s"\n    args: [%s]\n    cwd: "%s"'
                % (key, name, str(cmd).replace("\\", "/"),
                   ", ".join('"%s"' % a.replace("\\", "/") for a in argl),
                   str(cwd).replace("\\", "/")))
    if fmt == "cli":
        return " ".join(["<这个宿主自己的 CLI>", "mcp", "add", name, "--", cmd] + argl)
    return ""


def guess_format(path, raw=""):
    """按扩展名 + 内容猜格式（认不出来就返回 unknown，别硬猜）"""
    ext = os.path.splitext(path or "")[1].lower()
    if ext in (".json",):
        return "json"
    if ext in (".toml",):
        return "toml"
    if ext in (".yaml", ".yml"):
        return "yaml"
    s = (raw or "").lstrip()
    if s.startswith("{"):
        return "json"
    if re.search(r"^\s*\[?\[?\w", s, re.M):
        return "toml"
    if re.search(r"^\s*\w[\w.-]*\s*:", s, re.M):
        return "yaml"
    return "unknown"


def read_text(path):
    try:
        return io.open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return ""


def parse_ok(path, fmt, raw=None):
    """这个文件用对应解析器读得动吗（写完校验用）"""
    raw = read_text(path) if raw is None else raw
    try:
        if fmt == "json":
            json.loads(raw or "{}")
        elif fmt == "toml":
            if tomllib is None:
                return True
            tomllib.loads(raw or "")
        elif fmt == "yaml":
            if yaml is None:
                return True
            yaml.safe_load(raw or "") if raw.strip() else None
        return True
    except Exception:
        return False


def detect_shapes(path):
    """这份配置文件里有哪些"像 MCP 表"的东西（发现/报告用；不改文件）"""
    raw = read_text(path)
    fmt = guess_format(path, raw)
    hits = []
    for shape, key in (("mcpServers", "mcpServers"), ("mcp_servers", "mcp_servers"),
                       ("context_servers", "context_servers"), ("plugins_list", "plugins")):
        if fmt == "json":
            try:
                d = json.loads(raw or "{}")
                if isinstance(d, dict) and key in d:
                    hits.append((shape, key))
            except Exception:
                pass
        elif fmt == "toml":
            # 三种写法都算：[[plugins]] / [mcp_servers] / [mcp_servers.某个]（点号表）
            if re.search(r"^\s*\[\[?%s([.\]]|$)" % re.escape(key), raw, re.M):
                hits.append((shape, key))
        elif fmt == "yaml":
            if re.search(r"^%s\s*:" % re.escape(key), raw, re.M):
                hits.append((shape, key))
        elif key in raw:
            hits.append((shape, key))
    return fmt, hits


def entry_names(path, fmt, shape):
    """这个表里已经有哪些条目（拿来做幂等与"别冲掉别人"的对照）"""
    raw = read_text(path)
    out = []
    try:
        if fmt == "json":
            d = json.loads(raw or "{}")
            key = YAML_TOPKEYS.get(shape, shape)
            tbl = d.get(key) or {}
            out = list(tbl) if isinstance(tbl, dict) else []
        elif fmt == "toml" and tomllib is not None:
            d = tomllib.loads(raw or "")
            tbl = d.get(TOML_TABLES.get(shape, shape)) or {}
            if isinstance(tbl, list):                      # [[plugins]] 是数组
                out = [str(x.get("name") or x.get("id") or "") for x in tbl if isinstance(x, dict)]
            elif isinstance(tbl, dict):
                out = list(tbl)
        elif fmt == "yaml" and yaml is not None:
            d = yaml.safe_load(raw or "") or {}
            tbl = d.get(YAML_TOPKEYS.get(shape, shape)) or {}
            out = list(tbl) if isinstance(tbl, dict) else []
    except Exception:
        return []
    return [x for x in out if x]


def read_entry(path, fmt, shape, name, marker="agent_plugin.py"):
    """我们要的那条在不在、像不像"接平台的"（marker 是平台命令的特征串）"""
    raw = read_text(path)
    key = YAML_TOPKEYS.get(shape, shape)
    blob = ""
    try:
        if fmt == "json":
            d = json.loads(raw or "{}")
            blob = json.dumps((d.get(key) or {}).get(name) or {}, ensure_ascii=False)
        elif fmt == "toml" and tomllib is not None:
            d = tomllib.loads(raw or "")
            tbl = d.get(TOML_TABLES.get(shape, shape))
            if isinstance(tbl, list):
                for x in tbl:
                    if isinstance(x, dict) and (x.get("name") or x.get("id")) == name:
                        blob = json.dumps(x, ensure_ascii=False, default=str)
            elif isinstance(tbl, dict):
                blob = json.dumps(tbl.get(name) or {}, ensure_ascii=False, default=str)
        elif fmt == "yaml":
            # YAML 走文本判断更稳（避免 safe_load 把复杂结构吃掉）：找 name: 之后的这一段
            m = re.search(r"^%s\s*:(.*?)(?=^\S|\Z)" % re.escape(key), raw, re.S | re.M)
            seg = m.group(1) if m else ""
            m2 = re.search(r"^\s{2,}%s\s*:(.*?)(?=^\s{0,2}\S|\Z)" % re.escape(name), seg, re.S | re.M)
            blob = m2.group(1) if m2 else ""
    except Exception:
        blob = ""
    found = bool(blob)
    return found, (marker in blob if marker else found), blob[:200]


def _backup(path):
    try:
        shutil.copy2(path, path + ".bak-agentctl")
        return path + ".bak-agentctl"
    except Exception:
        return ""


def write_entry(path, fmt, shape, name, command, args, cwd, table=None):
    """把我们的条目写进去。返回 (ok, did, detail)。

    三条硬规矩：① 写前备份 ② 写完**立刻回读校验** ③ 校验不过就**回滚**（坏文件绝不留下）
    """
    raw = read_text(path)
    if not raw:
        return False, "文件读不到或为空", path
    if not parse_ok(path, fmt, raw):
        return False, "这份文件**本来就解析不过**（我们不动它）", path
    key = key_for(fmt, shape, table)
    existing = entry_names(path, fmt, shape)
    if name in existing:
        return True, "已在（跳过）", path
    bak = _backup(path)
    new = None
    # ── JSON：整份往返 ──
    if fmt == "json":
        try:
            d = json.loads(raw)
        except Exception as e:
            return False, "读不出 JSON：%s" % str(e)[:60], path
        d.setdefault(key, {})[name] = {"command": command, "args": list(args), "cwd": cwd}
        new = json.dumps(d, ensure_ascii=False, indent=2) + "\n"
    # ── TOML：优先 tomlkit（保注释）──
    elif fmt == "toml":
        if tomlkit is not None:
            try:
                doc = tomlkit.parse(raw)
                if shape == "plugins_list":
                    aot = doc.get(key)
                    if aot is None:
                        aot = tomlkit.aot()
                        doc[key] = aot
                    t = tomlkit.table()
                    t["name"] = name
                    t["command"] = command
                    t["args"] = list(args)
                    t["cwd"] = cwd
                    aot.append(t)
                else:
                    if key not in doc:
                        doc[key] = tomlkit.table()
                    t = tomlkit.table()
                    t["command"] = command
                    t["args"] = list(args)
                    t["cwd"] = cwd
                    doc[key][name] = t
                new = tomlkit.dumps(doc)
            except Exception as e:
                return False, "tomlkit 处理失败：%s" % str(e)[:60], path
        else:
            arr = "[" + ", ".join('"%s"' % a for a in args) + "]"
            if shape == "plugins_list":
                new = raw.rstrip("\n") + ('\n\n[[%s]]\nname = "%s"\ncommand = "%s"\nargs = %s\ncwd = "%s"\n'
                                          % (key, name, command.replace("\\", "\\\\"), arr, cwd.replace("\\", "\\\\")))
            else:
                new = raw.rstrip("\n") + ('\n\n[%s.%s]\ncommand = "%s"\nargs = %s\ncwd = "%s"\n'
                                          % (key, name, command.replace("\\", "\\\\"), arr, cwd.replace("\\", "\\\\")))
    # ── YAML：文本插入（只动目标那一块）──
    elif fmt == "yaml":
        lines = raw.split("\n")
        idx = None
        for i, l in enumerate(lines):
            if re.match(r"^%s\s*:" % re.escape(key), l):
                idx = i
                break
        ent = ["  %s:" % name, "    command: \"%s\"" % command.replace("\\", "/"),
               "    args: [%s]" % ", ".join('"%s"' % str(a).replace("\\", "/") for a in args),
               "    cwd: \"%s\"" % str(cwd).replace("\\", "/")]
        if idx is None:
            new = raw.rstrip("\n") + "\n" + key + ":\n" + "\n".join(ent) + "\n"
        else:
            tail = lines[idx].split(":", 1)[1].strip()
            if tail not in ("", "{}"):
                return False, "这个 %s: 是行内写法（%s），我们不猜着改" % (key, lines[idx].strip()[:40]), path
            new = "\n".join(lines[:idx + 1] + ent + lines[idx + 1:])
    else:
        return False, "这种格式(%s)不支持直接写" % fmt, path

    io.open(path, "w", encoding="utf-8", newline="\n").write(new)
    if not parse_ok(path, fmt):                      # ②③ 校验不过就回滚
        if bak:
            shutil.copy2(bak, path)
        else:
            io.open(path, "w", encoding="utf-8", newline="\n").write(raw)
        return False, "写完解析不过 → 已回滚（备份 %s）" % os.path.basename(bak or "（无）"), path
    lost = [x for x in existing if x not in entry_names(path, fmt, shape)]
    if lost:                                     # 别人的条目丢了 → 真的回滚（别只说）
        if bak:
            shutil.copy2(bak, path)
        else:
            io.open(path, "w", encoding="utf-8", newline="\n").write(raw)
        return False, "写完之后发现**别人的条目丢了**（%s）→ 已回滚" % ",".join(lost), path
    return True, ("写入" if shape not in ("plugins_list",) else "追加"), path
