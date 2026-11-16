# 接入手工单：{{name}}（{{id}}）

> **这份单子一次给全，不用回话**：
> · 全绿 → 停（什么都不用说）；
> · 有一步过不去 → **停下来，别硬接**：把原话/整段输出留在**你自己那边**（本地日志，或一个你自己写的文件），
>   并照第六节告诉你的主人卡在哪。

你还没接进「枢阁」。下面 = 内核这边的**规矩** + **现成模板** + **确切落点** + **验收标准**，
剩下的事**你全权负责**：找到你自己的配置、写进去、把接口起来、自测过。

- 平台：`{{platform}}`（内核 `core.exe`；控制台 <http://127.0.0.1:8795/>）
- 你的身份：`agent_id: {{agent_id}}`、`plugin_id: {{plugin_id}}`
{{id_note}}
- 接上之后你多一个工具：`brain_ops {op, args}` —— 读写主人笔记库
  （`{"op":"list"}` 能列出全部可用操作；`{"op":"brain.health"}` 是最小连通测试）

## 〇、先做三十秒自测（决定你走哪条分支，别问别人）

在本机跑下面三条（PowerShell 或等价方式），按结果对号入座：

```powershell
Test-Path '{{platform}}\scripts\agentctl.py'      # ① 平台在不在、你能不能读到/跑到它
Test-Path '{{config_probe}}'                             # ② 你的 MCP 配置文件在不在（我们看到的路径）
Get-Command python | Select-Object -ExpandProperty Source # ③ 有没有 python（要用 {{pyw}} 也行）
```

- ① 假 → 你够不到平台，**走分支 C**（把第五节那段交给主人）。
- ① 真、② 真 → **走分支 A**（最常见）。
- ① 真、② 假，但你有 hook / 事件回调 → **走分支 B**。
- 都没有 → **走分支 C**。

> 我们的判断：**这条很可能是「{{branch_guess}}」**（依据见第七节）。但以你自测为准。

## 一、规矩（八条，别自己发明）

1. **接口 = 一个常驻进程**（内核侧那半截）：
   `{{pyw}} {{agent_plugin}} --agent {{id}}`
2. **agent 侧 MCP 服务端**（你那半截）= 同一个文件加 `--mcp`，stdio：
   `{{pyw}} {{agent_plugin}} --agent {{id}} --mcp`
3. **事件上报（可选）**：`{{pyw}} {{hook_client}} --agent {{id}}`
   —— 从 **stdin 读一条 JSON** 投给平台（1 秒超时）。
4. ⚠️ **一律 `pythonw.exe`，不是 `python.exe`**：这些进程是你的宿主 spawn 的，我们给不了
   `CREATE_NO_WINDOW` —— 用 python.exe 就是每来一次闪一个黑框（主人硬要求：一个黑窗都不许有）。
   你自己拉进程也要 `DETACHED_PROCESS | CREATE_NO_WINDOW`（`0x08000000 | 0x00000008`）。
5. **两处"登记"，别混**：
   - **接口登记**（你在平台里存在这件事）—— 内核**重启时由接口进程自己**向内核注册
     （`POST /api/bridge/plugins/register`）。你要做的就是**跑一次 `activate`**；
     `verify` 说"接口已登记 ❌"就是这一步还没做。
   - **档位登记**（你能干什么）—— 落在 `data/plugins/permissions.json`：
     `read` / `write` / `trust`（累进：只读给 read，要写笔记给 write，删东西/碰私密才 trust）。
     在描述文件 `interface.tier` 里写一档（不写默认 `write`），**`add --apply` 会把它显式写进那个文件**。
   - ⚠️ 别拿内核 `/api/permissions` 当"登记过了"的证据 —— 它会把**默认档**也报出来。
     要看 `data/plugins/permissions.json` 里有没有你这个 plugin id。
6. **先找官方入口，再谈手改文件**：你能改自己的配置之前，先问一句"这东西有没有官方 CLI / 命令"。
   （反直觉的一条：有的宿主上"**显式申请写目录反而被拒**"（判成运行时状态路径）、**不申请时反倒能写** --
   别一看申请被拒就认定自己没权限，先用正常写入试一次。）
   （2026-10-05 的实例：Reasonix 明明有 `reasonix-cli.exe mcp add …`，对方却先花了好几轮去探路准备手改配置文件 ——
   官方入口一句命令就干完了，还自动写对位置、自动登记激活表。）
7. **别碰别人的东西**：只写 `agents/{{id}}.yaml` 和**你自己**的配置文件。
   `config.yaml`、档位这些**交给 CLI**（第二节 A2），别手改。
8. **按「格式 × 形状」写，别按印象写**：同样一件事（加一个 MCP 服务），Claude/Cursor 是 JSON 的
   `mcpServers`，Codex 是 TOML 的 `[mcp_servers.<名字>]`，Reasonix 是 TOML 的 `[[plugins]]`，
   Zed/Continue 是 YAML 的 `context_servers`。**写错形状 = 把人家已有的十几个服务一起冲掉**
   （对照表见 A1.5）。拿不准就问你自己宿主的官方 CLI（第 6 条）。

## 二、分支 A：你有 MCP 客户端配置，也能在本机写文件

### A1 写描述文件 `{{platform}}\agents\{{id}}.yaml`（唯一要手写的，已按我们看到的填好）

```yaml
id: {{id}}
name: {{name}}
agent_id: {{agent_id}}
plugin_id: {{plugin_id}}

detect:                       # 怎么认出"这台机器装了它"；~ 和 %ENV% 都认
  paths: [{{detect_paths}}]        # 目录
  files: [{{detect_files}}]        # 文件（(2) 别再塞进 paths）
  exes: [{{detect_exes}}]         # 可执行

interface:
  tier: write                 # 档位：read / write / trust（add --apply 会显式登记到 permissions.json）
  status_tool: {{id}}.status
  inbox: data/inbox/{{id}}
  record: data/transcripts/{{agent_id}}
  capabilities: [agent.bridge, chat, reasoning, tool-use]
  agent_capabilities: [chat, reasoning, tool-use]
  description: "{{name}} 接入通道：MCP 面给它软件操作。"

mcp:
  kind: {{mcp_kind}}          # mcp-json / dsh-plugin / claude-cli 三选一（你吃哪种看第三节）
  server_name: brain
  transport: stdio
  command: "{{pyw_slash}}"
  args: ["{{agent_plugin}}", "--agent", "{{id}}", "--mcp"]
  cwd: "{{platform_slash}}"
  config: "{{config_path}}"   # ← 你自己的 MCP 配置文件（绝对路径或 ~/…）
  entry_id: "mcpServers.brain"
  # 你如果有**好几个面**（例如命令行一份配置、GUI 另一份），用 hosts 逐个列，各写各的：
  # hosts:
  #   - {name: cli, kind: claude-cli}
  #   - {name: gui, format: json, shape: mcpServers, config: "%APPDATA%/你的宿主/config.json"}
  # format/shape 是**新写法**（不写 kind 也能接，任何格式×形状都认）；
  # 两个都不给时我们会**看文件自己认**（认不出就 none，不硬猜）。

hook:
  kind: none                  # {{hook_note}}
  config: ""
  command: ""
  client: "plugins/hook_client.py"    # (3) 填你**实际**用的那个 hook 客户端：自己的脚本就写它的路径
                                      #     （例：~/.reasonix/logs/hook_logger.py）。写错了平台会以为你没配 hook。

source:
  events: {{events_hint}}                # hook / agent-plugin / none（(3) 有 hook 证据时已预填）
  transcript: none
  adapter: ""
  path: ""
```

#### A1.5 你的面属于哪种「格式 × 形状」—— 照抄，别照错

各家写"同一个东西"的**形状不一样**（写错形状会把人家已有的服务一起冲掉）：

| 格式 | 表名 / 顶层键 | 谁是这样 |
|---|---|---|
{{format_matrix}}

- **你这一份**：`{{config_where}}`（我们判读成 `{{own_format}}`）。要加的这一条长这样：

```text
{{own_snippet}}
```

- 服务名固定 `brain`；`command` 用 `pythonw.exe`；`args` 里必须带 `--agent {{id}} --mcp`。
- ⚠️ 验收会在这个条目里找**平台特征串 `agent_plugin.py`** —— 光有 `brain` 这个名字不算接上
  （共享配置文件里可能只是别人的引用）。
- 上面这格是"我们判读的"，**跟你自己看到的不一样就以你为准**，并顺手把
  `{{config_where}}` 那一面的 `format`/`shape` 写准（我们判错了，别让下一个人再被带偏）。

### A2 剩下三处别手改，跑这四步（顺序不能换）

```bat
cd /d {{platform}}
:: 0) {{shell_step}}
python scripts\agentctl.py plan {{id}}          :: 只看它打算改哪几处（不落盘）
python scripts\agentctl.py add {{id}} --apply   :: 真写：config.yaml 条目 + 写你的 MCP 配置
python scripts\agentctl.py activate             :: 重启内核，让新接口生效（等它打印"内核回来了"）
python scripts\agentctl.py verify {{id}}        :: 验收
```

- 每动一个文件都会留 `.bak-agentctl` 备份；反复跑 `add` 是**幂等**的（会说"已在/已按模板更新"）。
- 写之前备份、写完**立刻用对应解析器回读校验**，解析不过就**回滚**：`add` 会明说
  "写完解析不过 → 已回滚" / "别人的条目丢了 → 已回滚"。看到这两句 = **原文件一个字节没变**，
  不是"写了一半"——别慌，改用手工片段或官方 CLI 再来。
- ⚠️ **`activate` 是全局重启**：它会把你**和所有别的 agent 的接口**一起重启（其他 agent 几秒内自己回来）。
  这是这一步唯一"动到别人"的地方 —— 知道它，别以为只重启了你。
- ⚠️ 我们第七节的分支判断和 `mcp` 建议**都只是猜的**。跟你自己看到的对不上 → **以你为准**，
  并且顺手把 `agents/_probe.yaml` 里那一条改准（我们的判断错了，别让下一个人再被带偏）。
- `verify` 里 **"接口已登记 ❌" = 内核还没重启**：跑 `activate`，等它回来说"内核回来了"，再 `verify` 一次。
- **两处登记**：`add` 会把档位**显式**写进 `data/plugins/permissions.json`（按 `interface.tier`，默认 write）；
  接口登记发生在 `activate`（内核一重启，接口进程自己注册）。
- `verify` 里 **"真调一次 brain.health ❌" = 你的 MCP 还没通**：去你那边确认 server 条目在里面、路径没错、
  然后**重启你自己**（MCP 配置一般要重启宿主才加载），再 `verify`。

### A3 自检清单（够了就停）

- [ ] `python scripts\agentctl.py verify {{id}}` 没有 ❌
- [ ] **新会话里**调一次 `brain_ops {"op": "brain.health"}` 并拿到回执
      （MCP 工具清单是**宿主启动时**加载的 -- 你这条会话里没有它很正常，别以为没接上；
       本会话想自证就自己当一次宿主走 stdio：initialize -> tools/list -> tools/call）
- [ ] 重启之后还在（内核会看着这个接口）

## 三、分支 B：你没有 MCP，但有 hook / 事件回调

1. 描述文件里的 `mcp.kind` 写 `none`，其余照 A1 抄；
2. 把这条命令挂到你自己的 hooks 上（每做完一件事调一次，stdin 给一条 JSON）：
   `{{pyw}} {{hook_client}} --agent {{id}}`
   —— 片段模板（形状按你自己的 hooks 格式套）：
   ```json
   { "hooks": { "PostToolUse": [ { "hooks": [ { "type": "command", "timeout": 10,
     "command": "{{pyw_slash}} {{hook_client_slash}} --agent {{id}}" } ] } ] } }
   ```
3. 照样跑 A2 那四步（`add` 会说 hook 那步跳过 —— 正常，hook 是你自己那边挂的）；
4. 自检：改一次你自己的东西，看 `{{platform}}\data\transcripts\agent.{{id}}\` 下有没有新文件。

## 四、分支 C：你两样都没有（或本地没有可写的配置）—— **别硬接**

你接不了，硬接只会留个空壳。你要做的只有两件：

1. 把**第五节那段原样交给主人**（自己不要改，那段是给他的操作说明）；
2. 把第六节那个 report 文件写下来（写清"我是 {{name}}，两样都没有"）。

## 五、给主人的一段（分支 C 专用，自包含，直接转发）

> **请你帮我接进「枢阁」**（本机知识库平台，在 `{{platform}}`）。我这边（{{name}}）
> 没有可写的 MCP 配置、也没有 hook，所以卡在"我这边没入口"。
> 你只需要做一件事：**在你的 {{name}} 里找 MCP / 扩展 / 插件的入口**，把下面这条加进去（服务名 `brain`）：
>
> ```json
> { "mcpServers": { "brain": { "command": "{{pyw_slash}}",
>   "args": ["{{agent_plugin}}", "--agent", "{{id}}", "--mcp"],
>   "cwd": "{{platform_slash}}" } } }
> ```
>
> - 如果你的 {{name}} 只吃**远程 MCP**（填 URL 那种），那这条先别接 —— 平台目前只提供本地 stdio 服务，
>   要开远程端点得另说；
> - 加完重启一次 {{name}}，然后回来让我自测 `brain_ops`；
> - 平台那边我需要的描述文件已经准备好了（见本单第二节 A1），你转给平台的人（或让我自己做）即可。

## 六、失败/做不下去怎么办（**不用回话**）

**停下来，别硬接**（硬接只会留个空壳）。把这几条写在**你自己那边**（本地日志、或你自己写的一个文件；
也可以直接讲给你的主人）—— 别往我们盘上写，我们看不到：
- 走到第几步、卡在哪（**原话 / 整段输出**贴上去）
- 你试过什么
- 你属于哪条分支（A/B/C）及依据
- 你还缺什么

各症状直接对照处理，别猜：

| 症状 | 怎么办 |
|---|---|
| `verify` 说"接口已登记 ❌" | 内核没重启 → 跑 `agentctl activate`，等"内核回来了"再 `verify` |
| `verify` 说"真调一次 brain.health ❌" | MCP 没通 → 确认你的 server 条目 + 重启宿主自己，再 `verify` |
| `add` 说"没找到配置文件（要手接）" | 你的 MCP 配置文件不存在 → 按 **A1.5** 建一个**跟你格式一致**的空壳再跑 |
| `add` 说"写完解析不过 → 已回滚" / "别人的条目丢了 → 已回滚" | 那份配置我们没写动（**原文件没变**）→ 拿 A1.5 的片段手工加，或走官方 CLI |
| `add` 说"这个形状我们**只读不写**" | 你这个形状我们故意不写（怕冲掉别人）→ 用你自己宿主的官方 CLI |
| 报"拒绝访问 / 权限不足" | 你写不进平台目录 → 走分支 C，把第五节给主人 |
| 你的配置是加密的 / 只在云端 | 走分支 C（本地没入口，硬接没用） |
| 命令找不到 / python 不对 | 用全路径 `{{pyw}}`，别靠 PATH |

## 七、我们这边看到的（供你定位自己的配置）

{{evidence}}
{{hint}}
{{note}}
{{branch_note}}