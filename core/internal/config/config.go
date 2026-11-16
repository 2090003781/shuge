// Package config 配置加载：解析 config.yaml（插件清单 + 资源声明）。
package config

import (
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

// Config 根配置
type Config struct {
	Bus       BusConfig      `yaml:"bus"`
	Plugins   []PluginConfig `yaml:"plugins"`
	Resources map[string]any `yaml:"resources"`
	Bridge    BridgeConfig   `yaml:"bridge"`
	Agent     AgentSection   `yaml:"agent"`
	Security  SecurityConfig `yaml:"security"`
}

// AgentSection agent 接入相关配置（章节）
// SecurityConfig 安全相关（2026-09-12 插件面鉴权之后新增）
type SecurityConfig struct {
	// AgentUplinkAuth agent 上报面（/api/bridge/send、/api/agent/session-start）的鉴权模式：
	//   soft（默认）—— 校验并**记账**但放行（迁移期：先看清谁没带 token，再转强制）
	//   strict       —— 不带/带错 token 直接 401（插件与 hook 都已带 token 后再开）
	//   off          —— 完全不校验（单机调试）
	AgentUplinkAuth string `yaml:"agent_uplink_auth"`
}

type AgentSection struct {
	SessionStart SessionStartConfig `yaml:"session_start"`
}

// SessionStartConfig 会话开始的上下文注入配置（2026-09-10 定稿）
type SessionStartConfig struct {
	ContextDir string               `yaml:"context_dir"` // 注入包目录（默认 data/context）
	Routes     []ContextRouteConfig `yaml:"routes"`      // agent → 注入内容路由
}

// ContextRouteConfig 单条注入路由
type ContextRouteConfig struct {
	Agent   string   `yaml:"agent"`
	Enabled bool     `yaml:"enabled"`
	Docs    []string `yaml:"docs"`
}

// BusConfig 事件总线配置
type BusConfig struct {
	QueueLen int `yaml:"queue_len"`
}

// BridgeConfig 中间平台（supervisor 主框架）配置
type BridgeConfig struct {
	Enabled bool `yaml:"enabled"` // 是否启用 bridge 主框架
	// Agents 骨架期预留：agent 适配器声明（未来从配置挂 agent 接入）
	Agents []AgentConfig `yaml:"agents"`
	// Modules 骨架期预留：声明挂载的功能模块（热缓存/RAG/规则……）
	Modules []ModuleConfig `yaml:"modules"`
}

// AgentConfig 单 agent 接入声明（骨架）
type AgentConfig struct {
	ID   string `yaml:"id"`
	Name string `yaml:"name"`
}

// ModuleConfig 单模块声明（骨架）
type ModuleConfig struct {
	ID      string `yaml:"id"`
	Enabled bool   `yaml:"enabled"`
}

// PluginConfig 单插件声明
type PluginConfig struct {
	ID         string            `yaml:"id"`
	Type       string            `yaml:"type"`   // builtin | external
	Module     string            `yaml:"module"` // builtin 插件注册名
	Enabled    bool              `yaml:"enabled"`
	Command    string            `yaml:"command"`    // external：可执行
	Args       []string          `yaml:"args"`       // external：参数
	WorkingDir string            `yaml:"cwd"`        // external：工作目录
	Supervisor *SupervisorConfig `yaml:"supervisor"` // external：监督策略
	Settings   map[string]any    `yaml:"settings"`   // 传给插件的自身配置
}

// SupervisorConfig 监督策略（可选，缺省用默认）
type SupervisorConfig struct {
	MaxRestarts   int `yaml:"max_restarts"`
	RestartWaitMs int `yaml:"restart_wait_ms"`
	KillTimeoutMs int `yaml:"kill_timeout_ms"`
}

// Load 从文件加载配置
func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("config read: %w", err)
	}
	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("config parse: %w", err)
	}
	if cfg.Bus.QueueLen <= 0 {
		cfg.Bus.QueueLen = 256
	}
	return &cfg, nil
}

// EnabledPlugins 返回启用的插件清单
func (c *Config) EnabledPlugins() []PluginConfig {
	out := make([]PluginConfig, 0, len(c.Plugins))
	for _, p := range c.Plugins {
		if p.Enabled {
			out = append(out, p)
		}
	}
	return out
}
