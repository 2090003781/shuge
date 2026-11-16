"""MCP 工具清单：把库的能力按"agent 会怎么用"包一层（不是把 34 个接口逐个搬出来）。

每项：
  name   工具名（agent 看到的名字）
  tier   权限档 read / write / danger —— **暴露与否在 config.json 里调**
  desc   写给 agent 看的一句话（什么时候用、返回什么、注意什么）
  schema 入参 JSON Schema
  call   落到哪个接口：{method, path(可带 {id}), query[], body[]}
"""
from __future__ import annotations

S = lambda **kw: {"type": "object", "properties": kw, "required": [k for k, v in kw.items()
                                                                 if v.pop("_req", False)]}
STR = {"type": "string"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}

TOOLS = [
    # ── 只读 ────────────────────────────────────────────────
    {"name": "search_notes", "tier": "read",
     "desc": "在枢阁里检索（关键词 + 语义 + 引用关系融合，可精排）。返回命中片段：文件名、"
             "路径、小节、位置（第几页）、命中通道。要整篇就再用 read_note。",
     "schema": {"type": "object", "required": ["q"], "properties": {
         "q": dict(STR, description="查询词"),
         "limit": dict(INT, description="返回几条，默认 8"),
         "scope": dict(STR, description="all=连归档日志一起搜（默认排除）"),
         "since": dict(STR, description="按笔记日期收窄：起（YYYY-MM-DD）"),
         "until": dict(STR, description="按笔记日期收窄：止"),
         "mode": dict(STR, description="kw=只走关键词（快，不加载模型）")}},
     "call": {"method": "GET", "path": "/api/search",
              "query": ["q", "limit", "scope", "since", "until", "mode"]}},

    {"name": "read_note", "tier": "read",
     "desc": "按库内路径取一篇的正文（可截断）。路径形如 知识库/01-编程通识/08-调试方法论。"
             "不知道路径就用 search_notes 或 list_tree。",
     "schema": {"type": "object", "required": ["path"], "properties": {
         "path": dict(STR, description="库内路径（扩展名可省）"),
         "max": dict(INT, description="最多取多少字符")}},
     "call": {"method": "GET", "path": "/api/content", "query": ["path", "max"]}},

    {"name": "list_tree", "tier": "read",
     "desc": "列出库的文件夹树（含每层文件、kind、是否在检索库里）。找路径时先用它。",
     "schema": {"type": "object", "properties": {}},
     "call": {"method": "GET", "path": "/api/tree"}},

    {"name": "note_detail", "tier": "read",
     "desc": "读一篇的元信息：标签、@引用（我引了谁）、反向链接（谁引了我）、派生摘要、块树统计。",
     "schema": {"type": "object", "required": ["id"], "properties": {"id": dict(STR, description="文件 id")}},
     "call": {"method": "GET", "path": "/api/files/{id}"}},

    {"name": "note_backlinks", "tier": "read",
     "desc": "谁引用了这一篇（可按小节筛）。做「这段是谁写的/被谁用到」这类问题时用它。",
     "schema": {"type": "object", "required": ["id"], "properties": {
         "id": dict(STR), "section": dict(STR, description="只看引这一节的")}},
     "call": {"method": "GET", "path": "/api/files/{id}/backlinks", "query": ["section"]}},

    {"name": "note_blocks", "tier": "read",
     "desc": "一篇文档的块树（页 / 块类型 / 标题层级 / 坐标 / 表格数据）。PDF 这类文档用它看结构。",
     "schema": {"type": "object", "required": ["id"], "properties": {
         "id": dict(STR), "page": dict(INT, description="只看某页"),
         "type": dict(STR, description="heading/paragraph/table/header/footer/figure")}},
     "call": {"method": "GET", "path": "/api/files/{id}/blocks", "query": ["page", "type"]}},

    {"name": "read_article", "tier": "read",
     "desc": "把一篇按「整篇」或「某一节」还原出来（给长文阅读 / 组上下文用）。",
     "schema": {"type": "object", "required": ["id"], "properties": {
         "id": dict(STR), "focus": dict(STR, description="聚焦到哪一节"),
         "section": dict(STR), "max_chars": dict(INT)}},
     "call": {"method": "GET", "path": "/api/files/{id}/article",
              "query": ["focus", "section", "max_chars"]}},

    {"name": "library_health", "tier": "read",
     "desc": "库的统计：文件数 / 块数 / 向量缺口 / 未入检索数 / 嵌入规格 / schema 版本。",
     "schema": {"type": "object", "properties": {}},
     "call": {"method": "GET", "path": "/api/health"}},

    {"name": "recent_events", "tier": "read",
     "desc": "增量读事件（谁在什么时候建/改/删/加锁…），可用于「最近动了什么」。",
     "schema": {"type": "object", "properties": {
         "after": dict(INT, description="从哪个序号之后"), "limit": dict(INT),
         "types": dict(STR, description="事件类型，逗号分隔，如 note.*")}},
     "call": {"method": "GET", "path": "/api/events", "query": ["after", "limit", "types"]}},

    {"name": "convert_engines", "tier": "read",
     "desc": "转换引擎状态：pandoc 版本、WPS 可用否、能写出哪些格式。",
     "schema": {"type": "object", "properties": {}},
     "call": {"method": "GET", "path": "/api/convert/engines"}},

    {"name": "run_selfcheck", "tier": "read",
     "desc": "跑一遍库自检（一致性 / 引用图 / 备份 / 卫生），返回逐项结论。",
     "schema": {"type": "object", "properties": {}},
     "call": {"method": "GET", "path": "/api/maintenance/selfcheck"}},

    # ── 写（不动原字节，有墓碑/原件兜底）──────────────────────
    {"name": "create_note", "tier": "write",
     "desc": "新建一篇笔记（Markdown）。名字写 .docx/.odt/.pdf 会现场生成真文档（这种默认不进检索库）。",
     "schema": {"type": "object", "required": ["name", "content"], "properties": {
         "name": dict(STR, description="含扩展名，如 研讨会记录.md"),
         "content": dict(STR, description="正文（Markdown）"),
         "folder_id": dict(STR, description="放哪个文件夹，默认根目录")}},
     "call": {"method": "POST", "path": "/api/files", "body": ["name", "content", "folder_id"]}},

    {"name": "update_note", "tier": "write",
     "desc": "保存一篇的正文（会重新切块、重建 @引用、补向量）。**只有文本格式能改**，"
             "文档格式（docx/pdf）要先 convert_note 转成 Markdown。",
     "schema": {"type": "object", "required": ["id", "content"], "properties": {
         "id": dict(STR), "content": dict(STR, description="新的完整正文")}},
     "call": {"method": "PUT", "path": "/api/files/{id}", "body": ["content"]}},

    {"name": "rename_note", "tier": "write",
     "desc": "改名。别人正文里对这个名字的引用会跟着改；指向它小节的引用也会跟随。",
     "schema": {"type": "object", "required": ["id", "name"], "properties": {
         "id": dict(STR), "name": dict(STR, description="新名字（含扩展名）")}},
     "call": {"method": "POST", "path": "/api/files/{id}/rename", "body": ["name"]}},

    {"name": "move_note", "tier": "write",
     "desc": "把一篇移到别的文件夹（按路径，路径不存在会建）。",
     "schema": {"type": "object", "required": ["id"], "properties": {
         "id": dict(STR), "path": dict(STR, description="目标库内路径，如 知识库/02-后端开发")}},
     "call": {"method": "POST", "path": "/api/files/{id}/move", "body": ["path", "folder_id"]}},

    {"name": "tag_note", "tier": "write",
     "desc": "给一篇加/去/设标签（标签也用于检索时的排除，如 logs 归档）。",
     "schema": {"type": "object", "required": ["id", "tags"], "properties": {
         "id": dict(STR), "tags": {"type": "array", "items": STR},
         "mode": dict(STR, description="add（默认）/ remove / set")}},
     "call": {"method": "POST", "path": "/api/files/{id}/tags", "body": ["tags", "mode"]}},

    {"name": "set_note_rag", "tier": "write",
     "desc": "把一篇加入或移出检索库。移出后文件还在，但搜不到（块与向量会被清掉）。",
     "schema": {"type": "object", "required": ["id", "on"], "properties": {
         "id": dict(STR), "on": dict(BOOL, description="true=加入检索库，false=移出"),
         "confirm": dict(BOOL, description="移出要带 true")}},
     "call": {"method": "POST", "path": "/api/files/{id}/rag", "body": ["on"]}},

    {"name": "convert_note", "tier": "write",
     "desc": "格式转换 / 导出：文档 → Markdown（to=md），或当前内容 → docx/pdf/odt/rtf/html/epub。"
             "产物是同目录的新文件，原件不动。",
     "schema": {"type": "object", "required": ["id", "to"], "properties": {
         "id": dict(STR), "to": dict(STR, description="md|docx|odt|rtf|html|epub|pdf"),
         "name": dict(STR, description="产物文件名（默认同名换扩展名）")}},
     "call": {"method": "POST", "path": "/api/files/{id}/convert", "body": ["to", "name"]}},

    {"name": "reindex", "tier": "write",
     "desc": "重建索引。默认只重建变过的（秒级）；with_vectors 顺带补向量缺口；full=true 才全量（慢）。",
     "schema": {"type": "object", "properties": {
         "reason": dict(STR), "with_vectors": dict(BOOL), "full": dict(BOOL),
         "confirm": dict(BOOL, description="全量重建要带 true")}},
     "call": {"method": "POST", "path": "/api/reindex", "body": ["reason", "with_vectors", "full"]}},

    # ── 危险（默认不暴露，要在 config.json 里放开）────────────
    {"name": "delete_note", "tier": "danger",
     "desc": "删除一篇（立墓碑，引用它的地方会显示「已删除」）。**不可逆**，务必先确认。",
     "schema": {"type": "object", "required": ["id"], "properties": {
         "id": dict(STR), "confirm": dict(BOOL, description="必须带 true")}},
     "call": {"method": "DELETE", "path": "/api/files/{id}"}},

    {"name": "set_note_visibility", "tier": "danger",
     "desc": "改可见性：public / internal / private（private 上锁后不进检索）。",
     "schema": {"type": "object", "required": ["id", "visibility"], "properties": {
         "id": dict(STR), "visibility": dict(STR), "confirm": dict(BOOL)}},
     "call": {"method": "POST", "path": "/api/files/{id}/visibility", "body": ["visibility"]}},

    {"name": "lock_vault", "tier": "danger",
     "desc": "立刻锁库（私密内容随即从检索里消失）。",
     "schema": {"type": "object", "properties": {"confirm": dict(BOOL)}},
     "call": {"method": "POST", "path": "/api/lock/lock"}},

    {"name": "unlock_vault", "tier": "danger",
     "desc": "解锁（会话级，默认 15 分钟）。",
     "schema": {"type": "object", "required": ["password"], "properties": {
         "password": dict(STR), "confirm": dict(BOOL)}},
     "call": {"method": "POST", "path": "/api/lock/unlock", "body": ["password"]}},
]

BY_NAME = {t["name"]: t for t in TOOLS}
