package api

// 软件操作目录 + 权限档（辞玖 2026-09-19：**所有操作封装在平台侧，不关 dsh 的事**）
//
//	· 目录：每个"软件操作"= 一个入口；按危险程度挂三档 read / write / trust
//	· 权限：按 **agent 接口插件** 存一个档位（plugin.dsh / plugin.reasonix …），
//	  落在 data/plugins/permissions.json；累积式——放到哪一档，就释放"该档及更低档"的入口
//	· 控制台：平台插件详情页里改（web/app.js），走 GET /api/permissions 与 POST /api/permissions/set
//
// 为什么在平台：平台才是"agent 与数据库/平台之间的接口"所在；dsh 那边不该掺一脚。

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
)

// OpEntry 一个软件操作入口。
type OpEntry struct {
	ID    string `json:"id"`    // 入口 id（平台侧统一用点号）
	Level string `json:"level"` // read | write | trust
	Op    string `json:"op"`    // 对应的软件操作（与库的接口清单一致）
	Label string `json:"label"` // 人看的一行
}

// LevelOrder 档位高低（累积释放靠它比大小）。
var LevelOrder = map[string]int{"read": 0, "write": 1, "trust": 2}

// LevelList 三档（控制台渲染与校验都用它）。
var LevelList = []map[string]string{
	{"id": "read", "label": "只读", "desc": "检索、读正文、看块树/反向链接/事件：无副作用"},
	{"id": "write", "label": "可写", "desc": "在只读之上：新建/改正文/改名/移动/标签/转换导出（产物是新文件，原件不动）"},
	{"id": "trust", "label": "相信", "desc": "在可写之上：进出检索库、重建索引、改可见性、删除、加锁解锁、设库密码"},
}

// OpCatalog 软件操作目录（平台侧唯一真源；与库的 docs/openapi.json 对得上）。
var OpCatalog = []OpEntry{
	// ── 只读 ──
	{"brain.search", "read", "GET /api/search", "检索（关键词+语义+引用关系融合）"},
	{"brain.read", "read", "GET /api/content", "按库内路径取正文"},
	{"brain.pack", "read", "POST /api/content/batch", "批量取正文（组注入包）"},
	{"brain.tree", "read", "GET /api/tree", "文件夹树"},
	{"brain.folders", "read", "GET /api/folders", "文件夹扁平表"},
	{"brain.tags", "read", "GET /api/tags", "标签统计"},
	{"brain.note", "read", "GET /api/files/{id}", "单篇元信息（标签/引用/反向链接/块树统计）"},
	{"brain.backlinks", "read", "GET /api/files/{id}/backlinks", "谁引用了这一篇"},
	{"brain.blocks", "read", "GET /api/files/{id}/blocks", "块树（页/类型/层级/坐标/表格）"},
	{"brain.article", "read", "GET /api/files/{id}/article", "按篇/按节还原"},
	{"brain.health", "read", "GET /api/health", "库统计"},
	{"brain.events", "read", "GET /api/events", "增量读事件"},
	{"brain.engines", "read", "GET /api/convert/engines", "转换引擎状态"},
	{"brain.selfcheck", "read", "GET /api/maintenance/selfcheck", "库自检"},
	{"brain.rag_list", "read", "GET /api/rag/list", "列出在/不在检索库的文件"},
	{"brain.properties", "read", "GET /api/properties", "按属性看全库（有哪些键/哪些值/各多少篇）"},
	{"brain.find_prop", "read", "GET /api/properties/find", "按属性找笔记（key 必填，value 空=只要有这个键）"},
	{"brain.writers", "read", "GET /api/files/{id}/writers", "这篇最近被谁改过"},
	{"brain.table_list", "read", "GET /api/tables", "列出数据表（表格线：csv/tsv/xlsx 建的表）"},
	{"brain.table_rows", "read", "GET /api/table_rows", "只读查询某张表（limit/offset/sort/desc/q）"},
	// ── 可写 ──
	{"brain.create", "write", "POST /api/files", "新建笔记（含现场生成 docx/pdf）"},
	{"brain.update", "write", "PUT /api/files/{id}", "保存正文（重新切块/引用/向量）"},
	// 写正文的另两种形态（2026-10-04 补）：整篇 set 之外还有"尾部追加"与"按小节改"
	{"brain.append", "write", "POST /api/files/{id}/append", "正文尾部追加一段（只写新增那段，不整篇重传）"},
	{"brain.section", "write", "POST /api/files/{id}/sections", "按小节改正文（replace/append/insert/delete）"},
	{"brain.prop_set", "write", "POST /api/files/{id}/properties", "改文件头属性（YAML）"},
	{"brain.rename", "write", "POST /api/files/{id}/rename", "改名（引用跟随）"},
	{"brain.move", "write", "POST /api/files/{id}/move", "移动到别的文件夹"},
	{"brain.tag", "write", "POST /api/files/{id}/tags", "加/去/设标签"},
	{"brain.new_folder", "write", "POST /api/folders", "新建文件夹"},
	{"brain.rename_folder", "write", "POST /api/folders/{id}/rename", "文件夹改名"},
	{"brain.convert", "write", "POST /api/files/{id}/convert", "格式转换/导出（产物是新文件）"},
	{"brain.table_drop", "write", "POST /api/table_drop", "删表（原文件还在，随时可重建）"},
	// ── 相信 ──
	{"brain.rag", "trust", "POST /api/files/{id}/rag", "加入/移出检索库"},
	{"brain.reindex", "trust", "POST /api/reindex", "重建索引（可全量）"},
	{"brain.reindex_one", "trust", "POST /api/files/{id}/reindex", "单篇重建派生层（不改正文）"},
	{"brain.relink_section", "trust", "POST /api/files/{id}/sections/relink", "修小节引用"},
	{"brain.replay_events", "trust", "GET /api/events/replay", "事件重放（可预演）"},
	{"brain.delete", "trust", "DELETE /api/files/{id}", "删除文件（立墓碑）"},
	{"brain.drop_folder", "trust", "DELETE /api/folders/{id}", "删空文件夹"},
	{"brain.visibility", "trust", "POST /api/files/{id}/visibility", "改可见性"},
	// 为什么这两条放最高档：
	//   · import 能把**任意本机路径**的内容读进库（等于读磁盘的能力），不该在"可写"档放开；
	//   · rag_batch 与单条的 brain.rag 同性质（决定哪些内容参与检索），同档。
	{"brain.import", "trust", "POST /api/import", "把磁盘上的文件/目录索引进库（默认不进检索）"},
	{"brain.table_import", "trust", "POST /api/tables", "从磁盘 csv/tsv/xlsx 建表（xlsx 每个 sheet 一张）"},
	{"brain.rag_batch", "trust", "POST /api/rag/batch", "批量进/出检索库（ids 或 folder_id）"},
	{"brain.lock", "trust", "POST /api/lock/lock", "锁库 / 解锁"},
	{"brain.set_password", "trust", "POST /api/lock/set", "设库密码"},
}

// DefaultLevel 新登记的 agent 接口插件默认放到哪一档。
const DefaultLevel = "write"

func permFile() string {
	return filepath.Join(PluginTrashDir, "permissions.json")
}

// LoadPermLevels 读 {插件 id: 档位}；文件不在或读坏 → 空表（调用方回落默认档）。
func LoadPermLevels() map[string]string {
	out := map[string]string{}
	raw, err := os.ReadFile(permFile())
	if err != nil {
		return out
	}
	_ = json.Unmarshal(raw, &out)
	return out
}

// permMu 让"读-改-写"整体串行：控制台连点、外部并发调用都不会互相覆盖或写出半个文件。
var permMu sync.Mutex

func savePermLevels(m map[string]string) error {
	if err := os.MkdirAll(filepath.Dir(permFile()), 0o755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	// 原子落盘：先写临时文件再改名，读的人永远看到完整 JSON
	tmp := permFile() + ".tmp"
	if err := os.WriteFile(tmp, append(data, '\n'), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, permFile())
}

// SetPermLevel 读-改-写（加锁）。返回落盘后的档位。
func SetPermLevel(plugin, level string) (string, error) {
	permMu.Lock()
	defer permMu.Unlock()
	levels := LoadPermLevels()
	levels[plugin] = level
	if err := savePermLevels(levels); err != nil {
		return "", err
	}
	return level, nil
}

// ReleasedOps 按档位算出放开的入口（累积：need <= cur）。
func ReleasedOps(level string) []string {
	cur, ok := LevelOrder[level]
	if !ok {
		cur = LevelOrder[DefaultLevel]
	}
	out := []string{}
	for _, e := range OpCatalog {
		if LevelOrder[e.Level] <= cur {
			out = append(out, e.ID)
		}
	}
	return out
}

// permissionBlock 给插件详情页用的权限块（只有 agent 接口插件才有意义）。
func (s *Server) permissionBlock(id string) map[string]any {
	levels := LoadPermLevels()
	level := levels[id]
	if _, ok := LevelOrder[level]; !ok {
		level = DefaultLevel
	}
	// 权限表（每条入口：档位 + 现在放没放开）
	released := map[string]bool{}
	for _, op := range ReleasedOps(level) {
		released[op] = true
	}
	ops := make([]map[string]any, 0, len(OpCatalog))
	for _, e := range OpCatalog {
		ops = append(ops, map[string]any{
			"id": e.ID, "level": e.Level, "op": e.Op, "label": e.Label,
			"released": released[e.ID],
		})
	}
	return map[string]any{
		"plugin":   id,
		"level":    level,
		"levels":   LevelList,
		"catalog":  ops,
		"released": len(released),
		"total":    len(OpCatalog),
	}
}

// agentInterfaceIDs 哪些插件算"agent 接口插件"：注册时自报了 agent_id 的。
func (s *Server) agentInterfaceIDs() []string {
	ids := []string{}
	if s.bridge == nil {
		return ids
	}
	for _, p := range s.mgr.All() {
		id, _ := p["id"].(string)
		if id == "" {
			continue
		}
		if reg := s.bridge.PluginByID(id); reg != nil && reg.Info().AgentID != "" {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	return ids
}

// handlePermissions GET /api/permissions[?plugin=xxx] —— 目录 + 各 agent 接口插件的档位
func (s *Server) handlePermissions(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimSpace(r.URL.Query().Get("plugin"))
	if id == "" {
		byPlugin := map[string]any{}
		for _, pid := range s.agentInterfaceIDs() {
			byPlugin[pid] = s.permissionBlock(pid)
		}
		writeJSON(w, map[string]any{
			"ok":       true,
			"levels":   LevelList,
			"plugins":  s.agentInterfaceIDs(),
			"byPlugin": byPlugin,
			"default":  DefaultLevel,
		})
		return
	}
	blk := s.permissionBlock(id)
	blk["ok"] = true
	writeJSON(w, blk)
}

// handlePermissionSet POST /api/permissions/set {plugin, level}
func (s *Server) handlePermissionSet(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Plugin string `json:"plugin"`
		Level  string `json:"level"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json: "+err.Error(), http.StatusBadRequest)
		return
	}
	body.Plugin = strings.TrimSpace(body.Plugin)
	body.Level = strings.TrimSpace(strings.ToLower(body.Level))
	if body.Plugin == "" {
		http.Error(w, "missing plugin", http.StatusBadRequest)
		return
	}
	if _, ok := LevelOrder[body.Level]; !ok {
		http.Error(w, "level must be read|write|trust", http.StatusBadRequest)
		return
	}
	if _, err := SetPermLevel(body.Plugin, body.Level); err != nil {
		http.Error(w, "save failed: "+err.Error(), http.StatusInternalServerError)
		return
	}
	blk := s.permissionBlock(body.Plugin)
	blk["ok"] = true
	writeJSON(w, blk)
}
