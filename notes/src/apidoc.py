"""**库内操作目录（唯一真源）** —— "这库能干什么"只在这里写一遍，别处都是它的派生物。

派生物（都由 `tools/api_spec.py` 生成或对账，**别手改**）：
  · `docs/openapi.json`  给 agent / 脚本 / 别的程序看
  · `docs/ops.json`      机器可读的完整目录（工具、校验、平台侧都读它）
  · 平台侧 `core/internal/api/permissions.go` 的 `OpCatalog`（agent 面）由 `--check` 对账：
    路由必须在目录里、档位必须一致（2026-10-04 起）

────────────────────────── 字段（**新增操作照这个填**） ──────────────────────────
  m      HTTP 方法：GET / POST / PUT / DELETE
  p      路径（`<id>` 是路径参数；query 不写进来，写在 q）
  sum    一句话说明（给人看，也给 agent 看）——**说清"干什么"，不是"怎么做"**
  tier   权限档（三档，累积）：
           read  = 只读：检索、读正文、看块树/反链/事件，无副作用
           write = 可写：新建/改正文/追加/改名/移动/标签/属性/转换（产物是新文件）
           trust = 相信：删除、可见性、进出检索库、重建索引、锁与密码、导入（能读任意本机路径）
         ⚠️ 名词跟平台 `OpCatalog` **必须一致**（历史叫法 `danger` 已统一成 `trust`）
  kind   这条操作动的是哪一层（决定它该不该出现在 agent 面）：
           data   = 库内数据（笔记/文件夹/标签/属性/检索/事件/表/锁）—— agent 面的来源
           engine = 格式引擎（docx/pdf：OOXML / PDF 对象模型，**不是文本原语的组合**）
           host   = 宿主能力（系统选择框、库文件夹路径配置）
           ops    = 运维（自检、重建索引、回填、维修、抓栈）
         没写 kind 的按 `kind_of(p)` 的规则推导；**推不出来的地方显式写**
  q      查询参数 {名: "类型 必填说明"}，类型取 str/int/bool
  b      请求体字段 {名: "类型 必填说明"}

────────────────────────── 新增一个操作（五步） ──────────────────────────
  1. `src/server.py` 实现路由：走 `self._send(...)`；写操作要落盘投影（`tree_sync`）+ 发事件；
     删除/覆盖类先留前像（`beforeimage.save`）；入参用 `as_int` / `BadRequest`
  2. **在本文件登记一条**（m/p/sum/tier/kind/q/b）—— 忘登记会被 `api_spec.py --check` 报红
  3. 要给 agent 用：`core/internal/api/permissions.go` 的 `OpCatalog` 加一行
     （**需要重编译内核并重启**；`--check` 会核对路由与档位）
  4. `python tools/api_spec.py --write && python tools/api_spec.py --check`
     （生成 openapi/ops.json；对账：实现了没登记、登记了没实现、档位/批量白名单不一致）
  5. 写回归 `tools/smoke_*.py` 并加进 `tools/run_all.py`

────────────────────────── 批量：一个入口，不许多开 ──────────────────────────
  能批量的操作写进下面的 `BATCH_OPS`，统一由 `POST /api/batch {op, ids, args}` 调用
  （实现在 server.py：**内部转发到单条路由**，所以新增操作零成本可批量）。
  **不要再为某个操作单开一个 `*_batch` 路由**（历史遗留的 `rag_batch` / `visibility_batch`
  保留可用，但新需求一律走 `/api/batch`）。
"""
from __future__ import annotations

ENTRIES = [
    # ── 只读 ────────────────────────────────────────────────
    {"m": "GET", "p": "/api/health", "sum": "存活与统计（文件/块/向量/未入检索/待补/清单）",
     "tier": "read"},
    {"m": "GET", "p": "/api/tree", "sum": "文件夹树（含每层文件；不含正文）", "tier": "read"},
    {"m": "GET", "p": "/api/folders", "sum": "文件夹扁平表（含路径，给移动用）", "tier": "read"},
    {"m": "GET", "p": "/api/tags", "sum": "标签统计", "tier": "read"},
{"m": "GET", "p": "/api/rag/list", "sum": "列出在/不在检索库的文件（on=1|0 过滤，带库内路径）",
 "q": {"on": "str 1=在检索库 0=不在", "limit": "int 条数"}, "tier": "read"},
{"m": "GET", "p": "/api/tables", "sum": "列出所有数据表（表格线：csv/tsv 建的表）", "tier": "read"},
{"m": "GET", "p": "/api/table_rows", "sum": "只读查询某张表（白名单 + 参数绑定）",
 "q": {"name": "str 必填 表名", "limit": "int 默认 200", "offset": "int 默认 0",
       "sort": "str 排序列", "desc": "str 1=倒序", "q": "str 过滤词"}, "tier": "read"},
    {"m": "GET", "p": "/api/search", "sum": "检索：关键词 + 向量 RRF（可 rerank / 时间收窄 / 排除归档）",
     "q": {"q": "str 必填 查询词", "limit": "int 默认8", "mode": "str kw=仅关键词",
           "scope": "str all=含归档", "since": "str 起（按笔记日期）", "until": "str 止",
           "expand": "str article=按篇召回", "n": "int 按篇召回想几篇",
           "rerank": "str 0/1 关开精排", "time": "str 0/1 同级时间优先",
           "exclude": "str 排除的标签，逗号分隔", "timing": "str 1=返回各段耗时"},
     "tier": "read"},
    {"m": "GET", "p": "/api/content", "sum": "按库内路径取正文（单条，可截断）",
     "q": {"path": "str 必填 库内路径", "max": "int 最多取多少字"}, "tier": "read"},
    {"m": "POST", "p": "/api/content/batch", "sum": "批量取正文（组「注入包」用）",
     "b": {"paths": "list 必填 库内路径数组", "max": "int 每条最多多少字"}, "tier": "read"},
    {"m": "GET", "p": "/api/files/<id>/writers", "sum": "这篇最近被谁改过（写入者标签，来源事件）", "tier": "read"},
    {"m": "POST", "p": "/api/visibility/batch",
     "sum": "**批量切可见性**：多选后一次把若干篇设为私密 / 移出私密"
            "（设为私密**锁着也能做**——用库公钥封内容密钥；移出私密要解密，必须先解锁）",
     "b": {"ids": "list 要处理的文件 id", "folder_id": "str 也可以整夹（配 recursive）",
           "recursive": "bool 整夹是否含子文件夹（默认 true）",
           "visibility": "str private|internal"}, "tier": "write"},
    {"m": "GET", "p": "/api/files/<id>/docx",
     "sum": "**DOCX 引擎读模型**：块（段落/表格/图片 + 样式与 run）+ 篇信息 + 渲染好的 HTML",
     "tier": "read"},
    {"m": "POST", "p": "/api/files/<id>/docx/edit",
     "sum": "**DOCX 引擎写回**：一批编辑操作（改字/改样式/增删移段/表格/查找替换/插图/页面设置），"
            "**原地改 OOXML，没碰到的样式与部件原样保留**",
     "b": {"ops": "list 必填 [{op,...}]；op=text|run|para|insert|del|move|cell|row|replace|image|page",
           "allow_shrink": "bool 新正文不足原文一半时要不要放行（默认 false 就拒绝）"}, "tier": "write"},
    {"m": "POST", "p": "/api/files/<id>/docx/export",
     "sum": "**导出成新文件**：md / txt / html（引擎抽）或 pdf（WPS 直接吃 docx，样式不简化）",
     "b": {"to": "str md|txt|html|pdf（默认 md）"}, "tier": "write"},
    {"m": "GET", "p": "/api/files/<id>/pdf",
     "sum": "**PDF 引擎总入口**：页 / 元信息 / 目录 / **批注清单** / 是不是扫描件", "tier": "read"},
    {"m": "GET", "p": "/api/files/<id>/pdf/text", "sum": "第 n 页的**文字层**（行 + span + bbox）",
     "q": {"n": "int 页码，从 1 起"}, "tier": "read"},
    {"m": "POST", "p": "/api/files/<id>/pdf/annot",
     "sum": "**加/删批注**（一次可多条，最后一起写回；先留前像）",
     "b": {"add": "list 要加的批注 [{page,kind,rects,text,color,author}]；kind=highlight/underline/"
                  "strikeout/squiggly/note/text/rect/ink",
           "del": "list 要删的批注 id（GET /pdf 的 annots[].id）"},
     "tier": "write"},
    {"m": "POST", "p": "/api/files/<id>/pdf/pages",
     "sum": "**页面操作**：rotate（转 90°）/ delete / move / extract（提取成新的一篇）",
     "b": {"op": "str rotate|delete|move|extract", "n": "int 第几页",
           "to": "int move 的目标位置", "nums": "list extract 要哪几页，如 [1,3]",
           "name": "str extract 出来的新文件名"}, "tier": "write"},
    {"m": "POST", "p": "/api/files/<id>/pdf/text-edit",
     "sum": "**擦字重画**：擦掉指定位置的原字、在原基线按原字体原字号重画新字（字体按用到的字**子集嵌入**）",
     "b": {"ops": "list 必填 [{page,rect:[x0,y0,x1,y1],text,size?,color?,font?,align?,shrink?}]；"
                  "空 text = 只擦不画"}, "tier": "write"},
    {"m": "POST", "p": "/api/files/<id>/pdf/replace",
     "sum": "**全篇查找替换**（命中的整行擦掉重画；没命中就不写回）",
     "b": {"q": "str 要找的", "rep": "str 换成什么（默认空 = 删掉）",
           "pages": "list 只在这几页里换（默认全篇）"}, "tier": "write"},
    {"m": "POST", "p": "/api/files/<id>/pdf/stamp",
     "sum": "**印记**：kind=pageno 盖页码（写进内容、文字层干净）/ kind=watermark 盖斜水印（**是图**）",
     "b": {"kind": "str pageno|watermark", "text": "str 水印文字",
           "start": "int 页码从几开始", "fmt": "str 页码格式（两个 %d = 当前页 / 总页数）",
           "size": "float 字号", "where": "str bottom-center|bottom-right|top-center",
           "color": "str #rrggbb 或 [r,g,b]", "margin": "float 距边（pt）",
           "opacity": "float 水印透明度 0~1", "angle": "float 水印角度",
           "pages": "list 只盖这几页（默认全篇）"}, "tier": "write"},
    {"m": "POST", "p": "/api/files/<id>/pdf/merge",
     "sum": "**合并**别的 PDF 进来（写回这一篇；要并的那几篇必须是 PDF）",
     "b": {"ids": "list 要并进来的文件 id（最多 10 篇）", "id": "str 只并一篇时的简写",
           "where": "str end|start|页码下标"}, "tier": "write"},
    {"m": "POST", "p": "/api/files/<id>/pdf/flatten",
     "sum": "**扁平化**：把批注烤进页面内容（⚠️ 这一页会变成图，不再是文字）",
     "b": {"dpi": "int 烤图分辨率（默认 150）", "pages": "list 只烤这几页（默认全篇有批注的页）"},
     "tier": "write"},
    {"m": "POST", "p": "/api/files/<id>/pdf/export-images",
     "sum": "**导出图片**：每页一张 PNG，**产出新文件**（不写回原篇）",
     "b": {"nums": "list 要导哪几页（默认全篇，一次最多 30 页）",
           "pages": "list 同 nums（兼容写法）", "dpi": "int 分辨率（默认 110）"}, "tier": "write"},
    {"m": "GET", "p": "/api/files/<id>/pages",
     "sum": "PDF 页清单（页码 / 宽高 / 每页字数；阅读器用）", "tier": "read"},
    {"m": "GET", "p": "/api/files/<id>/page", "sum": "PDF 第 n 页 → PNG（阅读器逐页渲染）",
     "q": {"n": "int 页码，从 1 起", "w": "int 目标宽度像素（默认 1200，200~4000）"}, "tier": "read"},
    {"m": "GET", "p": "/api/files/<id>/find", "sum": "PDF 全文查找：每处命中的页码 + 矩形 + 片段（跳页高亮用）",
     "q": {"q": "str 必填 要找的字"}, "tier": "read"},
    {"m": "POST", "p": "/api/files/<id>/writeback",
     "sum": "**文档编辑器保存**：把编辑器里的 Markdown 按原格式（docx/odt/rtf/html/epub/pdf）重新生成，写回同一篇（先留前像）",
     "b": {"content": "str 必填 正文（Markdown）", "allow_shrink": "bool 缩太多时的二次确认"},
     "tier": "write"},
    {"m": "GET", "p": "/api/files/<id>/raw", "sum": "原文件字节（图片/二进制直出，按扩展名给 Content-Type；加锁的 403）", "tier": "read"},
{"m": "GET", "p": "/api/files/<id>", "sum": "读一篇（正文 + @提及 + 反向链接 + 标签 + 派生摘要 + 块树统计 + 入库/修改时间）",
     "tier": "read"},
    {"m": "GET", "p": "/api/files/<id>/text", "sum": "取文档解析出来的 Markdown（?refresh=1 强制重解析）",
     "q": {"refresh": "str 1=重算"}, "tier": "read"},
    {"m": "GET", "p": "/api/files/<id>/blocks", "sum": "块树（页/块/类型/层级/bbox/表格）",
     "q": {"page": "int 只看某页", "page_no": "int 同上（兼容写法）",
           "type": "str 只看某类块"}, "tier": "read"},
    {"m": "GET", "p": "/api/files/<id>/backlinks", "sum": "反向链接（谁引用了我，可按小节筛）",
     "q": {"section": "str 只看引这一节的"}, "tier": "read"},
    {"m": "GET", "p": "/api/files/<id>/article", "sum": "按篇还原（整篇 / 某一节），给阅读与组包",
     "q": {"focus": "str 聚焦到哪一节", "q": "str 配合 focus 判断含词", "max": "int 整篇上限字数",
           "section": "str 取哪一节", "section_max": "int 单节上限字数"}, "tier": "read"},
    {"m": "GET", "p": "/api/events", "sum": "增量拉事件（位点续读）",
     "q": {"after": "int 从哪个 gseq 之后", "limit": "int 条数", "types": "str 事件类型，逗号分隔",
           "writer": "str 只看某个写入者"},
     "tier": "read"},
    {"m": "GET", "p": "/api/events/replay", "sum": "按 gseq 区间重放事件（dry_run=1 只算不写）",
     "q": {"from": "int", "to": "int", "types": "str", "dry_run": "str 1=只预演"}, "tier": "trust"},
    {"m": "GET", "p": "/api/convert/engines", "sum": "转换引擎状态（pandoc 版本 / WPS 可用否 / 能写出哪些格式）",
     "tier": "read"},
    {"m": "GET", "p": "/api/lock", "sum": "锁状态（configured / unlocked / expires_in）", "tier": "read"},
    {"m": "POST", "p": "/api/debug/stack-dump",
     "sum": "卡死抓栈：after 秒后把所有线程的栈打到日志（after=0 立即打）。**改成 POST**：它会写日志+装定时器",
     "b": {"after": "int 秒，默认 60", "repeat": "bool 默认 1"}, "tier": "write"},
    {"m": "GET", "p": "/api/properties", "sum": "按属性看全库（哪些键/哪些值/各多少篇）",
     "q": {"key": "str 只看某一个键"}, "tier": "read"},
    {"m": "GET", "p": "/api/properties/find", "sum": "按属性找笔记（key=必填，value 空=只要有这个键）",
     "q": {"key": "str 必填", "value": "str 可空", "limit": "int 默认 50",
           "offset": "int 默认 0"}, "tier": "read"},
    {"m": "POST", "p": "/api/files/<id>/properties", "sum": "改文件头 YAML 属性（与库元数据联动）",
     "b": {"set": "dict 要设的键值", "del": "list 要删的键"}, "tier": "write"},
    {"m": "POST", "p": "/api/maintenance/private-rekey",
     "sum": "一次性维修：密钥丢了读不出来的私密篇，用磁盘明文按当前主密钥重新加密（需已解锁）",
     "b": {"src": "str 可省：明文所在目录（默认先找 vault、再找库文件夹）"}, "tier": "write"},
    {"m": "POST", "p": "/api/maintenance/frontmatter-backfill",
     "sum": "一次性维护：把库里已有笔记的文件头 YAML 属性补解析一遍。**改成 POST，且范围必须显式**："
            "不给 limit 只补 100 篇，全库要 all:true（原来 GET 的 limit=0 会被兜成「全库」）",
     "b": {"limit": "int 最多扫多少篇（默认 100）", "all": "bool true=全库"}, "tier": "write"},
    {"m": "POST", "p": "/api/pick",
     "sum": "弹系统「选文件 / 选文件夹」框（本机窗口；立刻回 token，再用 GET /api/pick 轮询结果）",
     "b": {"kind": "str file|dir 默认 file", "title": "str 窗口标题",
           "dry_run": "bool 只验协议不弹窗（验收用）",
           "fake": "str 配合 dry_run：假装选了这个路径"}, "tier": "read"},
    {"m": "GET", "p": "/api/pick",
     "sum": "取系统选择框的结果：state=pending|ok|canceled|error，ok 时 paths 是选中的绝对路径",
     "q": {"token": "str 必填 从 POST /api/pick 拿"}, "tier": "read"},
    {"m": "GET", "p": "/api/maintenance/selfcheck", "sum": "一键自检（子进程跑 tools/selfcheck.py --json）",
     "tier": "read"},

    # ── 写（可撤销：不动原字节，有墓碑/版本兜底）────────────
    {"m": "POST", "p": "/api/files",
     "sum": "新建文件（**格式由 format 显式规定**；name 写 .docx/.pdf 等照样现场生成真文档）",
     "b": {"folder_id": "str 默认根目录", "name": "str 文件名（可含扩展名）",
           "content": "str 文本内容", "content_b64": "str 二进制内容（与 content 二选一）",
           "format": "str 指定格式：md/txt/json/jsonl/csv/tsv/yml/log/html/…（文本族，"
                     "库内可直接编辑）或 docx/odt/rtf/pdf/epub/html（文档族，pandoc/WPS 现场生成）。"
                     "规则：给了它就以它为准，和 name 里的扩展名不一致 → 400；"
                     "没给就按 name 的扩展名；**name 没有扩展名 → 默认 md**"},
     "tier": "write"},
    {"m": "PUT", "p": "/api/files/<id>", "sum": "保存正文（重新索引 + 重建 @提及边 + 补向量）",
     "b": {"content": "str 必填 新正文（文本格式；文档格式会被拒）",
           "allow_shrink": "bool 真允许「缩短很多」（默认拦，返回 shrink_refused）",
           "writer": "str 写入者标签（也可走 X-Writer 头）"}, "tier": "write"},
    {"m": "POST", "p": "/api/files/<id>/sections",
     "sum": "**按小节改正文**（写正文的第三种形态：整篇 set / 尾部 append / **按节** section）："
            "只动那一节，其余一个字不碰",
     "b": {"mode": "str 必填 replace=换这一节正文 / append=在节内末尾接 / insert=加一节 / delete=删这一节",
           "name": "str 必填 小节名（insert 时是新节名；匹配按库里的归一规则，整串优先）",
           "text": "str 新正文（replace/append/insert 用）",
           "new_name": "str replace 时顺手改名",
           "after": "str insert 插在哪一节后面（不给就放文末）",
           "level": "int insert 的标题层级（不给就跟 after 同级，还没有就 2）"}, "tier": "write"},
    {"m": "POST", "p": "/api/files/<id>/reindex",
     "sum": "**单篇重建派生层**（块/向量/派生；不改正文）—— 全库 reindex 太重，"
            "靠 PUT 重建又会改 mtime、留前像、发「改过了」的事件",
     "b": {"force_parse": "bool 文档格式：把解析结果丢掉重新解析",
           "with_vectors": "bool 顺带补这一篇的向量"}, "tier": "trust"},
    {"m": "POST", "p": "/api/files/<id>/append",
     "sum": "**正文尾部追加一段**（不是整篇替换）：给日志转录那种只会越长的篇用；"
            "盘上那份也按字节追加",
     "b": {"text": "str 必填 要追加的正文（文本类篇才给追加；文档类拒绝）"}, "tier": "write"},
    {"m": "POST", "p": "/api/files/<id>/rename", "sum": "改名（引用处跟着改，小节引用跟着跟随）",
     "b": {"name": "str 必填 新名"}, "tier": "write"},
    {"m": "POST", "p": "/api/files/<id>/move", "sum": "移动（按 folder_id 或 path）",
     "b": {"folder_id": "str", "path": "str 库内路径"}, "tier": "write"},
    {"m": "POST", "p": "/api/files/<id>/tags", "sum": "改标签",
     "b": {"tags": "list 必填", "mode": "str add|remove|set"}, "tier": "write"},
    {"m": "POST", "p": "/api/tables", "sum": "从磁盘 csv/tsv/xlsx 建表（xlsx 每个 sheet 一张；原文件同时留档、默认不进检索）",
 "b": {"src": "str 必填 磁盘路径", "under": "str 落到库内哪个目录", "title": "str 表标题",
       "sheet": "str 只取某个 sheet", "replace": "bool 重名时覆盖", "delimiter": "str csv 分隔符"},
 "tier": "trust"},
{"m": "POST", "p": "/api/table_drop", "sum": "删表（原文件还在，随时可重建）",
 "b": {"name": "str 必填 表名"}, "tier": "write"},
{"m": "POST", "p": "/api/rag/batch", "sum": "批量进/出检索库（on=true 时才嵌入）",
 "b": {"ids": "list 文件 id 数组", "folder_id": "str 整个文件夹",
       "on": "bool 必填 true=加入", "recursive": "bool 含子文件夹"},
 "tier": "trust"},
{"m": "POST", "p": "/api/import/upload",
 "sum": "浏览器选好文件后按字节导入（不需要磁盘路径；可带 rel_dir 保留目录层级）",
 "b": {"files": "list 必填 [{name, content_b64, rel_dir?}]（最多 500 个）",
       "under": "str 落到库内哪个目录", "rag": "bool 是否进检索（默认不进）",
       "private": "bool 按私密进库（需先解锁）"}, "tier": "write"},
{"m": "POST", "p": "/api/import", "sum": "把磁盘上的文件/目录索引进库（默认不进检索）",
 "b": {"src": "str 必填 磁盘路径（文件或目录）", "under": "str 落到库内哪个目录",
       "tag": "str 打标签", "rag": "bool 是否进检索（默认不进）",
       "overwrite": "bool 覆盖已存在的同名篇", "private": "bool 按私密进库（需先解锁）"},
 "tier": "trust"},
{"m": "GET", "p": "/api/before-images", "sum": "高风险操作（删除/覆盖）留下的前像清单（私密篇不留前像）", "q": {"file_id": "str 只看某篇", "limit": "int 默认 50"}, "tier": "read"},
{"m": "POST", "p": "/api/before-images/restore", "sum": "按前像恢复（文件还在就换回正文，已删除就重建）", "b": {"id": "str 前像 id", "file_id": "str 也可按篇取最近一条"}, "tier": "write"},
{"m": "POST", "p": "/api/before-images/prune", "sum": "清理旧前像（只删前像，不动正文；锁定时 403）", "b": {"days": "int 默认 30，至少 1"}, "tier": "write"},
{"m": "GET", "p": "/api/mirror", "sum": "库文件夹 / vault 的路径配置（历史名字带 mirror；**不是镜像管线**）", "tier": "read"},
{"m": "POST", "p": "/api/mirror/config", "sum": "设置镜像配置（只改传进来的那几个键）",
 "b": {"enabled": "bool 总开关：正式启用库后设 false，不再维持镜像管线",
       "library_dir": "str 库文件夹", "vault_dir": "str vault",
       "ignore": "list 忽略前缀", "private": "list 私密前缀（跳过同步）",
       "append_only": "list 只追加目录（永不覆盖）", "backup_root": "str 覆盖前备份到哪"},
 "tier": "write"},
{"m": "POST", "p": "/api/mirror/sync", "sum": "**已退役（2026-09-30 归档）**：回 410 Gone"
                                        "（配置里 enabled=false 时直接不跑，如实返回 disabled）",
 "b": {"apply": "bool 默认 true；false=只预演", "import": "bool 默认 true；false=只同步不导库"},
 "tier": "write"},
{"m": "POST", "p": "/api/files/<id>/rag", "sum": "进 / 出检索库（on=false 只存放，不清文件）",
     "b": {"on": "bool 必填 true=加入"}, "tier": "trust"},
    {"m": "POST", "p": "/api/files/<id>/convert", "sum": "格式转换 / 导出（to=md|docx|odt|rtf|html|epub|pdf）",
     "b": {"to": "str 目标格式", "name": "str 产物名"}, "tier": "write"},
    {"m": "POST", "p": "/api/files/<id>/sections/relink", "sum": "修正指向本篇小节（old→new）",
     "b": {"old": "str 必填", "new": "str 必填"}, "tier": "trust"},
    {"m": "POST", "p": "/api/folders", "sum": "新建文件夹",
     "b": {"parent_id": "str", "path": "str", "name": "str 必填"}, "tier": "write"},
    {"m": "POST", "p": "/api/folders/<id>/rename", "sum": "文件夹改名（引用处路径跟着改）",
     "b": {"name": "str 必填"}, "tier": "write"},
    {"m": "POST", "p": "/api/batch",
     "sum": "**批量**：把一个操作施加到一组目标（白名单见 `BATCH_OPS`；内部转发到单条路由，"
            "所以新操作登记进目录就自动可批量）",
     "b": {"op": "str 必填 操作名（rag/visibility/tag/move/properties/delete）",
           "ids": "list 目标文件 id（file 类操作）",
           "paths": "list 目标库内路径（path 类操作）",
           "args": "object 该操作的参数（原样转给单条路由）",
           "on_error": "str continue=出错也继续（默认）/ stop=第一处错就停",
           "dry_run": "bool 只列出要做什么，不动手"}, "tier": "trust"},
    {"m": "POST", "p": "/api/reindex", "sum": "重建索引（默认只重建变过的；full=true 才全量）",
     "b": {"reason": "str", "full": "bool 全量重建", "with_vectors": "bool 顺带补向量"}, "tier": "trust"},

    # ── 相信（trust：默认不给 agent，插件配置里才能放开）────
    {"m": "DELETE", "p": "/api/files/<id>", "sum": "删除文件（立墓碑，级联清派生层与引用边）",
     "tier": "trust"},
    {"m": "DELETE", "p": "/api/folders/<id>", "sum": "删空文件夹", "tier": "trust"},
    {"m": "POST", "p": "/api/files/<id>/visibility", "sum": "切可见性（public|internal|private）",
     "b": {"visibility": "str 必填"}, "tier": "trust"},
    {"m": "POST", "p": "/api/lock/set", "sum": "首次设库密码",
     "b": {"password": "str 必填", "confirm": "str 必填 确认密码"}, "tier": "trust"},
    {"m": "POST", "p": "/api/lock/reset",
     "sum": "重设库密码：清掉密钥材料（危险；**必须先解锁**才允许，忘密码走命令行工具）",
     "b": {"confirm": "bool 必填 true（确认知道后果）"}, "tier": "trust"},
    {"m": "POST", "p": "/api/lock/unlock",
     "sum": "解锁（会话级；默认不自动上锁，只手动锁。闲置超时：AI_NOTES_LOCK_TIMEOUT=<秒>）",
     "b": {"password": "str 必填"}, "tier": "trust"},
    {"m": "POST", "p": "/api/lock/lock", "sum": "立即上锁", "tier": "trust"},
{"m": "POST", "p": "/api/lock/change", "sum": "换库密码（先解后封：私密篇用新主密钥重新加密，旧密码随即失效）",
 "b": {"password": "str 新密码（至少 4 位）", "confirm": "str 再输一次"}, "tier": "trust"},
{"m": "POST", "p": "/api/lock/clear", "sum": "取消库密码（私密篇解密成明文并改成内部，然后清密钥材料）",
 "b": {"confirm": "bool 必填 true"}, "tier": "trust"},
]

# ── kind：这条操作动的是哪一层（推不出来的在条目里显式写 kind）─────────
_KIND_RULES = (
    ("host", ("/api/pick", "/api/mirror")),                      # 宿主能力
    ("ops", ("/api/debug/", "/api/maintenance/", "/api/reindex")),  # 运维
    ("engine", ("/pdf", "/docx", "/api/convert/engines")),        # 格式引擎
)


def kind_of(p: str) -> str:
    for k, pres in _KIND_RULES:
        if any(x in p for x in pres):
            return k
    return "data"                       # 其余都是库内数据操作


# ── 批量白名单（唯一真源）：`POST /api/batch {op, ids, args}` 能跑哪些操作 ──
#   target: file = 传 ids（文件 id）；path = 传 paths（库内路径）
#   route : 单条路由（`{id}` 会被替换成目标 id）
#   注意：批量入口本身登记成 trust 档（它能批量删），**要不要给 agent 由平台侧决定**
BATCH_OPS = {
    "rag":        {"target": "file", "method": "POST", "route": "/api/files/{id}/rag",
                   "sum": "进/出检索库（args: on=true|false）"},
    "visibility": {"target": "file", "method": "POST", "route": "/api/files/{id}/visibility",
                   "sum": "切可见性（args: visibility=public|internal|private）"},
    "tag":        {"target": "file", "method": "POST", "route": "/api/files/{id}/tags",
                   "sum": "改标签（args: tags=[], mode=add|remove|set）"},
    "move":       {"target": "file", "method": "POST", "route": "/api/files/{id}/move",
                   "sum": "移动（args: folder_id 或 path）"},
    "properties": {"target": "file", "method": "POST", "route": "/api/files/{id}/properties",
                   "sum": "改文件头属性（args: 属性键值）"},
    "delete":     {"target": "file", "method": "DELETE", "route": "/api/files/{id}",
                   "sum": "删除（立墓碑；先留前像由单条路由自己负责）"},
}

# 实际路由里必然出现的"模式"，用来和 server.py 对账（`<id>` 匹配任意段）
PATH_PARAM = "<id>"


def paths() -> set:
    return {"%s %s" % (e["m"], e["p"]) for e in ENTRIES}


def by_tier(tier: str) -> list:
    return [e for e in ENTRIES if e.get("tier") == tier]


def catalog() -> list:
    """规范化目录：每条都补齐 id / kind / batch 三样（`docs/ops.json` 与校验都读它）。

    · `id`   —— **唯一键就是 `METHOD 路径`**（平台侧的 `brain.*` 是另一套命名，只在那边有）
    · `kind` —— 条目里显式写了就用它，否则按 `kind_of()` 推
    · `batch`—— 能不能被 `/api/batch` 调（按 `BATCH_OPS` 的 route 反查）
    """
    batch_routes = {"%s %s" % (v["method"], v["route"].replace("{id}", "<id>"))
                    for v in BATCH_OPS.values()}
    out = []
    for e in ENTRIES:
        key = "%s %s" % (e["m"], e["p"])
        out.append({"id": key, "m": e["m"], "p": e["p"], "tier": e.get("tier") or "read",
                    "kind": e.get("kind") or kind_of(e["p"]),
                    "batch": key in batch_routes,
                    "sum": e.get("sum") or "", "q": e.get("q") or {}, "b": e.get("b") or {}})
    return out
