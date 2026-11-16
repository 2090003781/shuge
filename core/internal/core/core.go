// Package core 平台核心组装：EventBus + Registry + Env。
package core

import (
	"context"
	"log/slog"

	"core/internal/eventbus"
	"core/internal/plugin"
	"core/internal/registry"
)

// Core 平台核心：组装 EventBus / Registry / Env，提供生命周期。
type Core struct {
	Bus       *eventbus.Bus
	Registry  *registry.Registry
	Env       *plugin.Env
	log       *slog.Logger
	resources map[string]any
}

// Options 创建选项
type Options struct {
	QueueLen  int // 事件总线每订阅者缓冲
	Logger    *slog.Logger
	Resources map[string]any // 数据资源客户端（"rag"/"vault"/"sql"...）
}

// New 创建并初始化平台核心
func New(opts Options) *Core {
	log := opts.Logger
	if log == nil {
		log = slog.Default()
	}
	bus := eventbus.New(opts.QueueLen, log)
	reg := registry.New(log)
	res := opts.Resources
	if res == nil {
		res = make(map[string]any)
	}
	core := &Core{
		Bus:       bus,
		Registry:  reg,
		log:       log,
		resources: res,
	}
	core.Env = &plugin.Env{
		Bus:       busAdapter{bus: bus},
		Registry:  reg,
		Resources: resAdapter{res: res},
		Config:    nil,
	}
	return core
}

// Start 注册全部插件并启动（未来从配置文件加载）
func (c *Core) Start(ctx context.Context, plugins []plugin.Plugin) error {
	for _, p := range plugins {
		if err := c.Registry.Register(p); err != nil {
			c.log.Error("core: register failed", "id", p.ID(), "err", err)
			continue
		}
		if err := p.Init(ctx, c.Env); err != nil {
			c.log.Error("core: init failed", "id", p.ID(), "err", err)
			continue
		}
	}
	return c.Registry.StartAll(ctx, c.Env)
}

// Shutdown 优雅停止全部
func (c *Core) Shutdown(ctx context.Context) {
	c.Registry.StopAll(ctx)
}

// busAdapter 适配 eventbus.Bus → plugin.EventPublisher
type busAdapter struct{ bus *eventbus.Bus }

func (a busAdapter) Publish(ctx context.Context, e plugin.Event) error { return a.bus.Publish(ctx, e) }
func (a busAdapter) Subscribe(ctx context.Context, types []string, h plugin.Handler) (func(), error) {
	return a.bus.Subscribe(ctx, types, h)
}
func (a busAdapter) SubscribeNamed(ctx context.Context, name string, types []string, h plugin.Handler) (func(), error) {
	return a.bus.SubscribeNamed(ctx, name, types, h)
}

// resAdapter 适配资源 map → plugin.ResourceAccess
type resAdapter struct{ res map[string]any }

func (a resAdapter) Resource(name string) (any, error) {
	if r, ok := a.res[name]; ok {
		return r, nil
	}
	return nil, &ResourceNotFoundError{Name: name}
}

// ResourceNotFoundError
type ResourceNotFoundError struct{ Name string }

func (e *ResourceNotFoundError) Error() string { return "resource not found: " + e.Name }
