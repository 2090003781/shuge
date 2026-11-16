// 插件回收站持久化存储（data/plugins/trash.json + disabled.json）
// 语义（2026-09-06 辞宝定）：
//   - Delete（移入回收站）：插件停止并从列表消失；重启也不启动（config 声明无效化）→ trash.json
//   - Restore（恢复）：从回收站取出并重新启动
//   - Purge（永久删除）：从回收站彻底移除，进入 disabled.json（不可恢复、重启也不启动）
//
// 不再有"会话级删除重启恢复"——删除即持久化。
package api

import (
	"encoding/json"
	"os"
	"path/filepath"
	"time"

	"core/internal/config"
)

// PluginTrashDir 回收站目录（相对 core 运行目录）
var PluginTrashDir = filepath.Join("data", "plugins")

// TrashEntry 回收站条目：保存完整配置以便恢复
type TrashEntry struct {
	ID        string              `json:"id"`
	Name      string              `json:"name"`
	Type      string              `json:"type"`
	Config    config.PluginConfig `json:"config"`
	DeletedAt string              `json:"deletedAt"`
}

// trashStore trash.json/disabled.json 读写
type trashStore struct {
	trashFile    string
	disabledFile string
}

func newTrashStore() *trashStore {
	return &trashStore{
		trashFile:    filepath.Join(PluginTrashDir, "trash.json"),
		disabledFile: filepath.Join(PluginTrashDir, "disabled.json"),
	}
}

func (s *trashStore) ensureDir() error {
	return os.MkdirAll(filepath.Dir(s.trashFile), 0o755)
}

// loadTrash 读回收站（不存在返回空）
func (s *trashStore) loadTrash() (map[string]TrashEntry, error) {
	out := map[string]TrashEntry{}
	data, err := os.ReadFile(s.trashFile)
	if err != nil {
		if os.IsNotExist(err) {
			return out, nil
		}
		return nil, err
	}
	var list []TrashEntry
	if err := json.Unmarshal(data, &list); err != nil {
		return nil, err
	}
	for _, e := range list {
		out[e.ID] = e
	}
	return out, nil
}

// saveTrash 全量写回收站
func (s *trashStore) saveTrash(list []TrashEntry) error {
	if err := s.ensureDir(); err != nil {
		return err
	}
	data, err := json.MarshalIndent(list, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(s.trashFile, data, 0o644)
}

// loadDisabled 读永久禁用名单
func (s *trashStore) loadDisabled() (map[string]bool, error) {
	out := map[string]bool{}
	data, err := os.ReadFile(s.disabledFile)
	if err != nil {
		if os.IsNotExist(err) {
			return out, nil
		}
		return nil, err
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, err
	}
	return out, nil
}

func (s *trashStore) saveDisabled(m map[string]bool) error {
	if err := s.ensureDir(); err != nil {
		return err
	}
	data, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(s.disabledFile, data, 0o644)
}

// ---- 独立入口：main 启动循环前读取需要跳过的插件 ----

// SkipSet 需要跳过的插件 id 集合（回收站 + 永久禁用）
type SkipSet struct {
	ids map[string]bool
}

// LoadSkipSet 从默认目录读取回收站+禁用名单（main 在启动循环前调用）
func LoadSkipSet() (*SkipSet, error) {
	s := newTrashStore()
	trash, err := s.loadTrash()
	if err != nil {
		return nil, err
	}
	disabled, err := s.loadDisabled()
	if err != nil {
		return nil, err
	}
	ids := make(map[string]bool, len(trash)+len(disabled))
	for id := range trash {
		ids[id] = true
	}
	for id := range disabled {
		ids[id] = true
	}
	return &SkipSet{ids: ids}, nil
}

// Has 判断插件 id 是否应跳过启动
func (s *SkipSet) Has(id string) bool { return s.ids[id] }

// NowRFC3339 时间戳工具
func nowRFC3339() string { return time.Now().Format(time.RFC3339) }
