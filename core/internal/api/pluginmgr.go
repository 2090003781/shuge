// 本文件：插件运行时管理器（启停 + 回收站）。
// 语义（2026-09-06 辞宝定，取消会话级删除）：
//   - Stop：停掉运行中的插件（builtin 调 Stop 并从 registry 移除；external 调 supervisor.Stop），状态置 stopped
//   - Start：按保存的 PluginConfig 重建并启动（builtin 经 Factory.Create→Register→Init→Start；external 经 supervisor.Spawn）
//   - Delete：停掉并移入回收站（持久化 trash.json；重启也不启动，可 Restore）
//   - Restore / RestoreBatch：从回收站恢复并重新启动
//   - Purge / PurgeBatch：从回收站永久删除（进入 disabled.json；不可恢复，重启也不启动）
package api

import (
	"context"
	"fmt"
	"log/slog"
	"os/exec"
	"sync"
	"time"

	"core/internal/config"
	"core/internal/eventbus"
	"core/internal/plugin"
	"core/internal/registry"
	"core/internal/supervisor"
)

// PluginManager 插件运行时管理器
type PluginManager struct {
	mu       sync.Mutex
	log      *slog.Logger
	ctx      context.Context
	bus      *eventbus.Bus // 组件变更事件发布（SSE 推送前端自动刷新）
	reg      *registry.Registry
	sup      *supervisor.Supervisor
	env      *plugin.Env
	cfgs     map[string]config.PluginConfig // 启动时全部插件配置（按 ID）
	all      map[string]*pluginMeta         // 已知插件元信息（Stop 后仍在列表，Delete/入回收站 才移除）
	states   map[string]string              // id → running | stopped
	store    *trashStore                    // 回收站/禁用名单持久化
	trash    map[string]TrashEntry          // 回收站（内存态，Delete 写入）
	disabled map[string]bool                // 永久禁用名单（Purge 写入）
}

type pluginMeta struct {
	ID           string
	Name         string
	Type         string
	Capabilities []string
}

// NewPluginManager 创建管理器。cfgs 为启动加载的全部插件配置。
func NewPluginManager(ctx context.Context, log *slog.Logger, bus *eventbus.Bus,
	reg *registry.Registry, sup *supervisor.Supervisor,
	env *plugin.Env, cfgs []config.PluginConfig) *PluginManager {
	m := &PluginManager{
		log:      log,
		ctx:      ctx,
		bus:      bus,
		reg:      reg,
		sup:      sup,
		env:      env,
		cfgs:     make(map[string]config.PluginConfig),
		all:      make(map[string]*pluginMeta),
		states:   make(map[string]string),
		store:    newTrashStore(),
		trash:    map[string]TrashEntry{},
		disabled: map[string]bool{},
	}
	for _, pc := range cfgs {
		m.cfgs[pc.ID] = pc
	}
	// 加载回收站 + 永久禁用名单（内存态；启动过滤由 main 用 LoadSkipSet 处理）
	if t, err := m.store.loadTrash(); err == nil {
		m.trash = t
	}
	if d, err := m.store.loadDisabled(); err == nil {
		m.disabled = d
	}
	// 元信息：registry 中 builtin（有实例可取 Name/Type/Caps）
	for _, p := range reg.List() {
		m.all[p.ID()] = &pluginMeta{
			ID: p.ID(), Name: p.Name(), Type: string(p.Type()),
			Capabilities: p.Capabilities(),
		}
		m.states[p.ID()] = "running"
	}
	// external：supervisor 有进程的（config 无 Name/Caps 字段，用 ID 占位）
	if sup != nil {
		for _, st := range sup.Status() {
			id, _ := st["id"].(string)
			if id == "" {
				continue
			}
			if _, exists := m.all[id]; !exists {
				m.all[id] = &pluginMeta{ID: id, Name: id, Type: "external", Capabilities: []string{}}
			}
			m.states[id] = "running"
		}
	}
	return m
}

// All 全部已知插件（含 stopped；deleted 已移除）
func (m *PluginManager) All() []map[string]any {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := []map[string]any{}
	for id, meta := range m.all {
		status, ok := m.states[id]
		if !ok {
			status = "running"
		}
		out = append(out, map[string]any{
			"id": meta.ID, "name": meta.Name, "type": meta.Type,
			"capabilities": meta.Capabilities, "status": status,
		})
	}
	return out
}

// State 查询某插件状态；未记录但存在则 running
func (m *PluginManager) State(id string) string {
	m.mu.Lock()
	defer m.mu.Unlock()
	if s, ok := m.states[id]; ok {
		return s
	}
	if m.reg.Get(id) != nil {
		return "running"
	}
	return "unknown"
}

// Stop 停掉插件（不重建，保留配置可 Start）
func (m *PluginManager) Stop(id string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if s, ok := m.states[id]; ok && (s == "stopped" || s == "deleted") {
		return nil // 幂等
	}
	pc, ok := m.cfgs[id]
	if !ok {
		return fmt.Errorf("plugin not configured: %s", id)
	}
	// 进程外插件：监督器 Stop（主动停不重启；未管理=已停，忽略）
	if pc.Type == "external" && m.sup != nil {
		_ = m.sup.Stop(id)
	}
	// 进程内插件：Stop + 从 registry 移除
	if p := m.reg.Get(id); p != nil {
		_ = p.Stop(m.ctx)
		_ = m.reg.Unregister(id)
	}
	m.states[id] = "stopped"
	m.log.Info("pluginmgr: stopped", "id", id)
	m.publish("platform.plugin.stopped", id)
	return nil
}

// publish 发布组件变更事件（SSE 推送前端自动刷新；bus 可能为空）
func (m *PluginManager) publish(typ, id string) {
	if m.bus == nil {
		return
	}
	_ = m.bus.Publish(m.ctx, plugin.NewEvent(typ, "pluginmgr",
		map[string]any{"id": id, "component": "plugin"}))
}

// Start 按配置重建并启动插件
// supervisorTracks 监督器现在是否真的在管这个进程（崩溃预算耗尽后它会从表里删除）
func (m *PluginManager) supervisorTracks(id string) bool {
	if m.sup == nil {
		return false
	}
	for _, st := range m.sup.Status() {
		if sid, _ := st["id"].(string); sid == id {
			return true
		}
	}
	return false
}

func (m *PluginManager) Start(id string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	pc, ok := m.cfgs[id]
	if !ok {
		return fmt.Errorf("plugin not configured: %s", id)
	}
	// 已在运行则幂等——但"状态表说 running"不算数（2026-09-12 检修实测）：
	// 插件崩溃且监督器重启预算耗尽后，监督器会把进程从表里删掉，而这里的状态表还停在 running，
	// 于是控制台点「启用」变成空操作、用户只能重启内核才能恢复。判据改成问监督器实况。
	if s, ok := m.states[id]; ok && s == "running" && m.supervisorTracks(id) {
		return nil
	}
	// 清理可能残留的旧实例
	if old := m.reg.Get(id); old != nil {
		_ = old.Stop(m.ctx)
		_ = m.reg.Unregister(id)
	}
	switch pc.Type {
	case "builtin":
		// 内置插件位已清空（2026-09-12 检修）：平台不再把能力编进内核，全部走 external 插件进程
		return fmt.Errorf("builtin 插件位已清空：%s 请改用 external 插件", pc.ID)
	case "external":
		if m.sup == nil {
			return fmt.Errorf("no supervisor for external plugin: %s", id)
		}
		cmd := exec.Command(pc.Command, pc.Args...)
		if pc.WorkingDir != "" {
			cmd.Dir = pc.WorkingDir
		}
		policy := supervisor.DefaultPolicy()
		if pc.Supervisor != nil {
			if pc.Supervisor.MaxRestarts > 0 {
				policy.MaxRestarts = pc.Supervisor.MaxRestarts
			}
			if pc.Supervisor.RestartWaitMs > 0 {
				policy.RestartWait = time.Duration(pc.Supervisor.RestartWaitMs) * time.Millisecond
			}
			if pc.Supervisor.KillTimeoutMs > 0 {
				policy.KillTimeout = time.Duration(pc.Supervisor.KillTimeoutMs) * time.Millisecond
			}
		}
		if _, err := m.sup.Spawn(id, cmd, policy); err != nil {
			return fmt.Errorf("spawn: %w", err)
		}
		if _, exists := m.all[id]; !exists {
			m.all[id] = &pluginMeta{ID: id, Name: id, Type: "external", Capabilities: []string{}}
		}
	default:
		return fmt.Errorf("unknown plugin type: %s", pc.Type)
	}
	m.states[id] = "running"
	m.log.Info("pluginmgr: started", "id", id)
	m.publish("platform.plugin.started", id)
	return nil
}

// Reload 重读一份插件清单，把**新增**的条目并进启动快照并启动它们；**在跑的一律不动**。
//
// 为什么需要它（2026-10-06 辞玖："必须重启内核是浏览器的限制吗"）：不是浏览器的问题 ——
// cfgs 是**启动时快照**，`/api/plugins/start` 只认快照里的 id，所以新加一条（agent 接入就是
// 这么加的）必须重启**整个内核**，把所有 agent 接口一起打断 3~5 秒。agent 接入是随时会发生的
// 操作，不该让人肉去点重启。
//
// 边界（有意如此，别扩）：**只加不减** —— 从 config.yaml 里删掉的条目这里不碰（要停/删走
// /api/plugins/stop|delete，那是显式动作）；回收站/永久禁用名单里的条目照样跳过。
func (m *PluginManager) Reload(entries []config.PluginConfig) map[string]any {
	m.mu.Lock()
	fresh, skipped := []config.PluginConfig{}, []string{}
	for _, pc := range entries {
		if m.disabled[pc.ID] {
			skipped = append(skipped, pc.ID)
			continue
		}
		if _, inTrash := m.trash[pc.ID]; inTrash {
			skipped = append(skipped, pc.ID)
			continue
		}
		if _, known := m.cfgs[pc.ID]; known {
			skipped = append(skipped, pc.ID)
			continue
		}
		m.cfgs[pc.ID] = pc // 先并进快照：Start 就是按它建进程的
		fresh = append(fresh, pc)
	}
	m.mu.Unlock()

	added, started, errs := []string{}, []string{}, map[string]string{}
	for _, pc := range fresh {
		added = append(added, pc.ID)
		if err := m.Start(pc.ID); err != nil {
			errs[pc.ID] = err.Error()
			continue
		}
		started = append(started, pc.ID)
	}
	if len(added) > 0 {
		m.log.Info("pluginmgr: reload", "added", added, "started", len(started), "errors", len(errs))
	}
	return map[string]any{"added": added, "started": started, "unchanged": skipped, "errors": errs}
}

// Delete 停掉并移入回收站（持久化；可 Restore，重启不自动启动）
func (m *PluginManager) Delete(id string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.deleteLocked(id)
}

func (m *PluginManager) deleteLocked(id string) error {
	if _, inTrash := m.trash[id]; inTrash {
		return nil // 已在回收站，幂等
	}
	pc, ok := m.cfgs[id]
	if !ok {
		// config 无此插件但运行中（如 external 手工 spawn）：仍允许入回收站（配置占位）
		pc = config.PluginConfig{ID: id, Type: "external"}
	}
	// 记录回收站条目（含完整配置以便恢复）
	m.trash[id] = TrashEntry{
		ID: id, Name: id,
		Type:      pc.Type,
		Config:    pc,
		DeletedAt: nowRFC3339(),
	}
	if err := m.saveTrashLocked(); err != nil {
		return err
	}
	// 停掉并移除
	if pc.Type == "external" && m.sup != nil {
		_ = m.sup.Stop(id)
	}
	if p := m.reg.Get(id); p != nil {
		_ = p.Stop(m.ctx)
		_ = m.reg.Unregister(id)
	}
	delete(m.all, id)
	delete(m.states, id)
	delete(m.disabled, id) // 从禁用名单收回（恢复用）
	m.log.Info("pluginmgr: moved to trash", "id", id)
	m.publish("platform.plugin.deleted", id)
	return nil
}

// Restore 从回收站恢复并启动
func (m *PluginManager) Restore(id string) error {
	m.mu.Lock()
	entry, ok := m.trash[id]
	if !ok {
		m.mu.Unlock()
		return fmt.Errorf("plugin not in trash: %s", id)
	}
	delete(m.trash, id)
	if err := m.saveTrashLocked(); err != nil {
		m.mu.Unlock()
		return err
	}
	// 补回配置（config 里可能已无，用回收站保存的配置）
	if _, ok := m.cfgs[id]; !ok {
		m.cfgs[id] = entry.Config
	}
	m.mu.Unlock()
	err := m.Start(id)
	m.log.Info("pluginmgr: restored", "id", id, "err", err)
	return err
}

// RestoreBatch 批量恢复
func (m *PluginManager) RestoreBatch(ids []string) ([]string, error) {
	done := []string{}
	for _, id := range ids {
		if err := m.Restore(id); err == nil {
			done = append(done, id)
		}
	}
	return done, nil
}

// Purge 从回收站永久删除（进入禁用名单，重启不启动、不可恢复）
func (m *PluginManager) Purge(id string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.trash[id]; !ok {
		return fmt.Errorf("plugin not in trash: %s", id)
	}
	delete(m.trash, id)
	if err := m.saveTrashLocked(); err != nil {
		return err
	}
	m.disabled[id] = true
	if err := m.saveDisabledLocked(); err != nil {
		return err
	}
	m.log.Info("pluginmgr: purged permanently", "id", id)
	return nil
}

// PurgeBatch 批量永久删除
func (m *PluginManager) PurgeBatch(ids []string) ([]string, error) {
	done := []string{}
	for _, id := range ids {
		if err := m.Purge(id); err == nil {
			done = append(done, id)
		}
	}
	return done, nil
}

// TrashList 回收站列表（前端展示用）
func (m *PluginManager) TrashList() []map[string]any {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := []map[string]any{}
	for _, e := range m.trash {
		out = append(out, map[string]any{
			"id": e.ID, "name": e.Name, "type": e.Type, "deletedAt": e.DeletedAt,
		})
	}
	return out
}

// saveTrashLocked 写回收站（需持锁）
func (m *PluginManager) saveTrashLocked() error {
	list := make([]TrashEntry, 0, len(m.trash))
	for _, e := range m.trash {
		list = append(list, e)
	}
	return m.store.saveTrash(list)
}

func (m *PluginManager) saveDisabledLocked() error {
	return m.store.saveDisabled(m.disabled)
}
