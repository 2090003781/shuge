// Package registry 插件注册表：注册/查找/生命周期管理。
package registry

import (
	"context"
	"log/slog"
	"sync"

	"core/internal/plugin"
)

// Registry 插件注册表
type Registry struct {
	mu      sync.RWMutex
	plugins map[string]plugin.Plugin
	order   []string // 启动顺序（注册顺序）
	log     *slog.Logger
}

// New 创建注册表
func New(log *slog.Logger) *Registry {
	if log == nil {
		log = slog.Default()
	}
	return &Registry{plugins: make(map[string]plugin.Plugin), log: log}
}

// Register 注册插件（不启动）。重复 ID 返回错误。
func (r *Registry) Register(p plugin.Plugin) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, exists := r.plugins[p.ID()]; exists {
		return &DuplicateError{ID: p.ID()}
	}
	r.plugins[p.ID()] = p
	r.order = append(r.order, p.ID())
	r.log.Info("registry: registered", "id", p.ID(), "type", p.Type())
	return nil
}

// Unregister 卸载插件（需先 Stop）
func (r *Registry) Unregister(id string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, exists := r.plugins[id]; !exists {
		return &NotFoundError{ID: id}
	}
	delete(r.plugins, id)
	for i, oid := range r.order {
		if oid == id {
			r.order = append(r.order[:i], r.order[i+1:]...)
			break
		}
	}
	r.log.Info("registry: unregistered", "id", id)
	return nil
}

// Get 按 ID 查找
func (r *Registry) Get(id string) plugin.Plugin {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.plugins[id]
}

// List 列出全部插件
func (r *Registry) List() []plugin.Plugin {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]plugin.Plugin, 0, len(r.order))
	for _, id := range r.order {
		if p, ok := r.plugins[id]; ok {
			out = append(out, p)
		}
	}
	return out
}

// StartAll 顺序启动全部插件（失败不影响已启动的，记录错误继续 = 隔离）
func (r *Registry) StartAll(ctx context.Context, env *plugin.Env) error {
	var firstErr error
	for _, p := range r.List() {
		if err := p.Start(ctx); err != nil {
			r.log.Error("registry: start failed", "id", p.ID(), "err", err)
			if firstErr == nil {
				firstErr = err
			}
			continue // 隔离：一个失败不阻止其他
		}
		r.log.Info("registry: started", "id", p.ID())
	}
	return firstErr
}

// StopAll 逆序优雅停止全部插件
func (r *Registry) StopAll(ctx context.Context) {
	ps := r.List()
	for i := len(ps) - 1; i >= 0; i-- {
		p := ps[i]
		if err := p.Stop(ctx); err != nil {
			r.log.Error("registry: stop error", "id", p.ID(), "err", err)
		}
	}
}

// DuplicateError 重复注册错误
type DuplicateError struct{ ID string }

func (e *DuplicateError) Error() string { return "plugin already registered: " + e.ID }

// NotFoundError 未找到错误
type NotFoundError struct{ ID string }

func (e *NotFoundError) Error() string { return "plugin not found: " + e.ID }
