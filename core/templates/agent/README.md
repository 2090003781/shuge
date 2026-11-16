# agent 接入模板（统一模板 · 一处描述，多处生成）

> 辞玖 2026-10-05 定的形状：
> **不管哪个 agent，接入都照这个模板来；而且有一个程序能自动认到目标 agent，
> 按模板把"内核侧接口 + agent 侧 MCP/hook"生成出来、初始化并接上。**

## 一、一块地只有一处要手写：`agents/<id>.yaml`

| 字段 | 干什么 | 例 |
|---|---|---|
| `id` | agent 短名（也是描述文件名） | `dsh` |
| `agent_id` / `plugin_id` | 平台里的身份 / 内核侧接口插件 id | `agent.dsh` / `plugin.dsh` |
| `detect` | **自动识别**：`paths` / `files` / `exes`，任一命中就算这台机器装了它 | `~/.dsh`、`~/.dsh/profiles/web/cordis.patch.yml` |
| `interface` | 内核侧接口参数（inbox / record / capabilities / status_tool） | 见下 |
| `mcp` | agent 侧 MCP 怎么接（`kind` 或**新写法** `format`+`shape`） | `dsh-plugin` / `mcp-json` / `codex-toml` / `format: yaml, shape: context_servers` |
| `hook` | agent 侧 hook 怎么接（没有就 `none`） | `claude-settings` / `none` |
| `source` | 日志/事件从哪来（转录用） | `session-store` / `hook` / `agent-plugin` / `none` |

## 二、生成物（都由模板渲染，**不要手改生成物**）

```
agents/<id>.yaml
   │  scripts/agentctl.py plan <id>     ← 先看要写哪几处（不落盘）
   │  scripts/agentctl.py add  <id> --apply
   ├─▶ config.yaml              加一条内核侧接口（templates/agent/plugin-entry.yaml.tpl）
   │      → plugins/agent_plugin.py --agent <id>        ← 通用运行时，**不为每个 agent 写代码**
   ├─▶ data/plugins/permissions.json   登记档位（默认 write；trust 由主人在控制台调）
   ├─▶ agent 侧 MCP 配置（按 mcp.kind 选模板）
   │      · dsh-plugin  → ~/.dsh/profiles/web/cordis.patch.yml   (mcp-<id> 条目)
   │      · mcp-json    → ~/.claude.json  mcpServers.<name>       (Claude Code / 通用 JSON 客户端)
   │      · codex-toml  → ~/.codex/config.toml  [mcp_servers.<name>]
   │      · none        → 该 agent 不吃 MCP（**别拿 reasonix 当例子**：它有 [[plugins]] 外部 MCP 表，2026-10-05 订正过），跳过
   └─▶ agent 侧 hook（按 hook.kind 选模板；MCP 与 hook 可并存）
          · claude-settings → ~/.claude/settings.json 的 hooks（命令指到 plugins/hook_client.py）
          · none            → 跳过
```

**agent 侧那两个脚本也是通用的**（不为每个 agent 复制一份）：
- `plugins/agent_plugin.py --agent <id> --mcp` —— MCP 服务端（stdio；同一个文件也是内核侧常驻接口）
- `plugins/hook_client.py --agent <id>` —— hook 客户端（stdin 收一条 JSON → 交 agent 接口）

## 二·五、"格式 × 形状"：同一件事各家写法不一样（**别按印象写**）

"加一个 MCP 服务"在不同宿主里是**不同的格式 + 不同的表名**。写错形状 = 把人家已有的十几个服务一起冲掉，
所以这一层单独抽出来：`scripts/mcphosts.py`（判读 `face_of` / 出片段 `snippet` / 落盘 `write_entry`）。

| 格式 | 表名 / 顶层键 | 谁是这样 | 我们写吗 |
|---|---|---|---|
| JSON | `mcpServers` | Claude Desktop / Cursor / Cline / WorkBuddy | ✅ 整份往返 |
| JSON | `context_servers` | Zed | ✅ |
| TOML | `[mcp_servers.<名字>]` | Codex CLI | ✅（有 `tomlkit` 就保注释） |
| TOML | `[[plugins]]` | Reasonix（External MCP servers） | ⛔ **只读**：用它自己的 `reasonix-cli.exe mcp add` |
| YAML | `mcpServers:` / `mcp_servers:` | 一些 YAML 系宿主 | ✅ 文本插入（YAML 没有保注释的写入器） |
| YAML | 顶层数组 / `- insert:` 列表（`- id:` 条目） | DSH(cordis.patch.yml) | ✅（插进现成的 `- insert:`，缩进跟里面条目一致） |
| 命令 | —— | Claude Code（`claude mcp add`） | ✅ 走它自己的 CLI |

**三种判法，优先级从高到低**（`mcp_hosts` 把结果挂在每个面上：`format` / `shape` / `writable` / `how`）：

1. 面里**显式**写 `format` + `shape`（新写法，没见过的宿主也能接）；
2. 老字段 `kind`（`mcp-json` / `codex-toml` / …）查表 —— 老描述文件不用改；
3. 两个都没给 → **看文件自己认**（按扩展名猜格式 + 探有没有 MCP 表）；认不出就 `none`，**不硬猜**。

**落盘三条硬规矩**（`mcphosts.write_entry`，JSON/TOML/YAML 全走同一条路）：
① 写前备份 `.bak-agentctl`；② 写完**立刻用对应解析器回读校验**；③ 不过、或者**发现别人的条目丢了** → **回滚**。
所以失败时那句"已回滚"= **原文件一个字节没变**，不是"写了一半"。

> 单元：`python scripts/smoke_mcphosts.py`（9 种情形 + 面判读 7 例）
> 端到端：`python scripts/sim_foreign.py`（**假电脑**上 10 种形状真跑 detect → plan → add → verify，含幂等）

## 三、加一个新 agent（照这个顺序）

1. 复制 `agents/dsh.yaml` 成 `agents/<新id>.yaml`，填 `detect` 与 `interface`（MCP/hook 按它支持什么填）
2. `python scripts/agentctl.py scan` —— 确认**认到了**（认不到就补 `detect`）
3. `python scripts/agentctl.py plan <新id>` —— 看它准备写哪几处（拿不准先干跑）
4. `python scripts/agentctl.py add <新id> --apply` —— 真写（所有被改的文件都留 `.bak-agentctl` 备份）
5. `python scripts/agentctl.py activate`（走启停台重启内核）+ `verify <新id>` —— 验收

> 想**先在一台假电脑上试**（不动真库真配置）：`python scripts/sim_foreign.py`。
> 它拿一份假 `HOME/APPDATA` + 临时 `config.yaml`/`permissions.json`，把整条链跑一遍，
> 跑完比对哈希确认**真的没碰本机**。

## 四、验收标准（`agentctl verify <id>` 逐条打）

- 内核侧：`/api/components` 里有 `plugin_id` 且 `status=running`
- 能力齐：`agent.deliver` · `agent.record.<x>` · `agent.attach.<x>` · `agent.detach.<x>` · `<x>.status`
- 档位：`/api/permissions` 读得到（默认 write）
- **真调一次**：`brain.health` 经该 agent 的接口走通（证明工具面活着）
- agent 侧：按**那个面的 `format` × `shape`** 精确读回，确有条目**且带平台特征**（`agent_plugin.py`）
  —— 判据只有 `_face_state()` 一份，写 / 验收 / 列表共用（`ok` 是**读回来**的，不看话术前缀）

## 五、两条不许破的规矩

1. **内核侧接口只有一个进程**（`plugins/agent_plugin.py`，由内核 Supervisor 保活）。
   agent 侧的 MCP/hook 是**另一个进程**（由 agent 自己 spawn），两者共用同一个 `plugin_id`
   —— 于是身份、档位、工具面、日志入口都在一处，不会两处各读一份漂移。
2. **生成物必须能由描述文件重算**：`plan` 的输出 == `add --apply` 写进去的东西。
   （改了模板或描述文件 → 重跑 `add`，幂等；校验见 `scripts/agentctl.py verify`）

## `interface.tier`：档位也要**登记**

同一个 agent 有两处"登记"，别混：

- **接口登记**：内核重启时由接口进程自己 `POST /api/bridge/plugins/register`（跑 `agentctl activate` 就是做这个）；
- **档位登记**：写进 `data/plugins/permissions.json` —— 描述文件 `interface.tier`（`read`/`write`/`trust`，不写默认 `write`），
  `agentctl add --apply` 会**显式**登记；**已在文件里的档位不会被 add 改动**（比如 dsh 的 trust 保得住）。

⚠️ 内核 `/api/permissions` 会把**默认档**一起报出来（看着像"登记过了"）—— 判断"登记没登记"要直接看那个文件。

## 一个 agent 多个面：`mcp.hosts`

同一个 agent 常常有好几个"面"，**每个面读自己的配置**（接了 CLI 不等于接了 GUI）：

- Claude Code **CLI** → `~/.claude.json`（走它自己的 `claude mcp add`）
- Claude **Desktop** → `%APPDATA%/Claude/claude_desktop_config.json`（另一个文件）
- Codex CLI → `~/.codex/config.toml`；WorkBuddy → `~/.workbuddy/.mcp.json`；DSH → `~/.dsh/profiles/web/cordis.patch.yml`

所以 `mcp:` 底下可以列 `hosts:` —— `agentctl add` **逐个写**，`verify` **每个面一行**（文件不存在的算"没装/跳过"，不算失败）；
不写 `hosts` 就当**一个面**（老描述文件不用改，面名 `default`）。

```yaml
mcp:
  kind: claude-cli                       # 默认接法；面可以覆盖 kind / config / entry_id
  server_name: brain
  command: "{{pyw}}"
  args: ["{{core}}/plugins/agent_plugin.py", "--agent", "<id>", "--mcp"]
  cwd: "{{core}}"
  hosts:
    - {name: claude-code-cli, kind: claude-cli}
    - {name: claude-desktop, kind: mcp-json, config: "%APPDATA%/Claude/claude_desktop_config.json"}
    # 新写法：不写 kind，直接声明格式与形状（没见过的宿主也能接）
    - {name: zed,   format: json, shape: context_servers, config: "%APPDATA%/Zed/settings.json"}
    - {name: other, format: yaml, shape: mcpServers,      config: "~/x/config.yaml"}
```

面板上那一格会显示 `MCP 1/2 面（claude-desktop 没装）`，鼠标停上去能看到每个面各自在不在位。

## 可选：`guard` 段（守护插件看住它）

写了这一段，[plugin.agent-guard](../../plugins/agent_guard_plugin.py) 就会把"这个 agent 没在跑就拉起来"
当成自己的活；**没写就完全不碰**。开关另存一份，默认关（控制台点「守护：开」，或 `agentctl guard <id> on`）。

```yaml
guard:
  probe: {kind: tcp, port: 3080}       # tcp=端口有人听；proc=进程名在（tasklist）；不写 kind 就不守护
  launcher:                            # 怎么拉起来（默认关着，开了才动）
    cmd: "cmd.exe"
    args: ["/c", "%APPDATA%\\npm\\dsh.cmd", "web"]
    cwd: "~"
  kill:                                # guard.restart 用它：杀掉 → 等守护拉回来
    kind: cmdline
    proc: node.exe
    match: "*deepseek-ai\\dsh*"
    not_match: "*subprocess-local*"
```

- 拉起固定走 `DETACHED_PROCESS | CREATE_NO_WINDOW`（**结构性不弹黑窗**，同内核 `prepareCmd`）。
- 探针失败要**连续两次**才认定"没在跑"，免得端口抖一下就误拉。
- 台账 + 开关落在 `data/agents/guard.json`；`guard.status/guard.set/guard.restart` 三个工具在控制台面板上。

> ⚠️ agent 侧起这个 MCP 服务端必须用 **pythonw.exe**（不是 python.exe）：它是 agent 自己 spawn 的，
> 我们给不了 CREATE_NO_WINDOW —— 用 python.exe 每来一次就是一闪一个黑框。
> 体检：`python scripts/check_no_windows.py`。
