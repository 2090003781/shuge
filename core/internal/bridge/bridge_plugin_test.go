// 本文件：bridge 关键不变量的回归测试（2026-09-12 补测试网）。
//
// 为什么挑这几条：它们都是"错了会**静默**坏掉"的路径——投递投不到、路由找不到、
// 注册重复污染身份，界面上都看不出来。今天这些行为靠手工验过一遍，这里把它们钉住。
package bridge

import (
	"context"
	"io"
	"log/slog"
	"testing"
	"time"

	"core/internal/eventbus"
	"core/internal/plugin"
)

// fakeCaller 记录"平台 → 插件"的调用（真投递是 HTTP 长轮询，测试里只关心调用形状）
type fakeCaller struct {
	calls   []string // pluginID|tool
	lastEv  map[string]any
	failFor map[string]bool
}

func (f *fakeCaller) CallPlugin(ctx context.Context, pluginID, tool string, args map[string]any, timeout time.Duration) (map[string]any, error) {
	f.calls = append(f.calls, pluginID+"|"+tool)
	if ev, ok := args["event"].(map[string]any); ok {
		f.lastEv = ev
	}
	if f.failFor[pluginID] {
		return nil, context.DeadlineExceeded
	}
	return map[string]any{"ok": true}, nil
}

func newTestBridge(t *testing.T) (*Bridge, *fakeCaller) {
	t.Helper()
	b := New(Options{})
	fc := &fakeCaller{failFor: map[string]bool{}}
	b.SetPluginCaller(fc)
	return b, fc
}

func reg(id, name string, caps []string, agentID string) PluginRegistration {
	return PluginRegistration{
		ID: id, Name: name, Capabilities: caps, Description: name + " 的说明",
		AgentID: agentID, AgentName: agentID, AgentCapabilities: caps,
		AgentDescription: agentID + " 的身份说明", DeliverTool: "agent.deliver",
	}
}

// 注册幂等 + 身份声明是插件自己的字段（2026-09-12 辞玖拍板的模型）
func TestRegisterPluginIdempotentAndCarriesAgent(t *testing.T) {
	b, _ := newTestBridge(t)

	isNew, err := b.RegisterPlugin(reg("plugin.dsh", "DSH 通道", []string{"agent.bridge"}, "agent.dsh"))
	if err != nil || !isNew {
		t.Fatalf("首次注册应 isNew=true 且无错：isNew=%v err=%v", isNew, err)
	}
	isNew2, err := b.RegisterPlugin(reg("plugin.dsh", "DSH 通道", []string{"agent.bridge"}, "agent.dsh"))
	if err != nil || isNew2 {
		t.Fatalf("重复注册应 isNew=false（幂等）：isNew=%v err=%v", isNew2, err)
	}
	if len(b.Plugins()) != 1 {
		t.Fatalf("重复注册不该产生第二条：%d", len(b.Plugins()))
	}
	info := b.Plugins()[0]
	if info.AgentID != "agent.dsh" || info.AgentDescription == "" {
		t.Fatalf("身份声明应挂在插件上：%+v", info)
	}
	// 身份**不再**作为独立 agent 实体出现（辞玖 2026-09-12 定：身份申明与通道放一起）
	for _, a := range b.Agents() {
		if a.ID == "agent.dsh" {
			t.Fatalf("agent.dsh 不应作为独立 agent 出现在 Agents() 里：%+v", a)
		}
	}
}

// 定向投递：target 既可以是插件 id，也可以是它声明的身份
func TestInjectByPluginIDAndByAgentID(t *testing.T) {
	b, fc := newTestBridge(t)
	if _, err := b.RegisterPlugin(reg("plugin.dsh", "DSH 通道", []string{"agent.bridge"}, "agent.dsh")); err != nil {
		t.Fatal(err)
	}
	ev := plugin.NewEvent("test.ev", "console", map[string]any{"n": 1})

	if err := b.Inject(context.Background(), "agent.dsh", ev); err != nil {
		t.Fatalf("按身份投递失败：%v", err)
	}
	if err := b.Inject(context.Background(), "plugin.dsh", ev); err != nil {
		t.Fatalf("按插件 id 投递失败：%v", err)
	}
	if len(fc.calls) != 2 {
		t.Fatalf("应有两次投递，实际 %v", fc.calls)
	}
	if fc.lastEv["type"] != "test.ev" {
		t.Fatalf("投递载荷应带事件类型：%+v", fc.lastEv)
	}
	// 投错人不该投：不存在的 target
	if err := b.Inject(context.Background(), "agent.nobody", ev); err != nil {
		t.Fatalf("投给不存在的目标不该报错（只是没人收）：%v", err)
	}
	if len(fc.calls) != 2 {
		t.Fatalf("不该多出投递：%v", fc.calls)
	}
}

// 投递失败的**契约是留痕**，不是返回错误：Inject 不打断调用方（定向推送是尽力而为），
// 但必须在总线上发 platform.inject.failed——"推给谁失败了、什么类型" 可查。
// （我第一版测试按"应返回错误"写，跑出来才发现契约是留痕；这正是测试网的价值。）
func TestInjectFailureLeavesTrace(t *testing.T) {
	bus := eventbus.New(8, slog.New(slog.NewTextHandler(io.Discard, nil)))
	b := New(Options{Bus: bus})
	fc := &fakeCaller{failFor: map[string]bool{"plugin.dsh": true}}
	b.SetPluginCaller(fc)
	if _, err := b.RegisterPlugin(reg("plugin.dsh", "DSH 通道", nil, "agent.dsh")); err != nil {
		t.Fatal(err)
	}

	got := make(chan plugin.Event, 4)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if _, err := bus.SubscribeNamed(ctx, "test-failed-trace", []string{"platform.inject.failed"},
		func(_ context.Context, ev plugin.Event) error { got <- ev; return nil }); err != nil {
		t.Fatal(err)
	}

	if err := b.Inject(ctx, "agent.dsh", plugin.NewEvent("test.ev", "console", nil)); err != nil {
		t.Fatalf("Inject 不该因投递失败而报错（那是 best-effort）：%v", err)
	}
	select {
	case ev := <-got:
		if ev.Payload["target"] != "agent.dsh" {
			t.Fatalf("留痕应带 target：%+v", ev.Payload)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("投递失败没有留下 platform.inject.failed 痕迹")
	}
}

// 未装配调用通道时报错要清楚（而不是静默丢）
func TestSendWithoutCallerFails(t *testing.T) {
	p := &ExternalPlugin{id: "plugin.x", agentID: "agent.x", deliverTol: "agent.deliver", timeout: time.Second}
	if err := p.Send(context.Background(), plugin.NewEvent("t", "s", nil)); err == nil {
		t.Fatal("没有调用通道时应报错")
	}
}

// 工具路由：能力名 == 工具名（内核内模块清零后，插件接活的唯一通路）
func TestToolRoutingByCapability(t *testing.T) {
	b, _ := newTestBridge(t)
	if _, err := b.RegisterPlugin(reg("plugin.note-search", "笔记查找", []string{"note.search"}, "")); err != nil {
		t.Fatal(err)
	}
	id, ok := b.externalPluginFor("note.search")
	if !ok || id != "plugin.note-search" {
		t.Fatalf("应路由到 plugin.note-search：id=%q ok=%v", id, ok)
	}
	if _, ok := b.externalPluginFor("note.delete"); ok {
		t.Fatal("没声明过的工具不该路由到任何插件")
	}
}

// 按 agent 身份找插件（详情页与投递共用）
func TestPluginByAgent(t *testing.T) {
	b, _ := newTestBridge(t)
	if _, err := b.RegisterPlugin(reg("plugin.rx", "Reasonix 通道", nil, "agent.reasonix")); err != nil {
		t.Fatal(err)
	}
	if p := b.PluginByAgent("agent.reasonix"); p == nil || p.ID() != "plugin.rx" {
		t.Fatalf("按身份应找到插件：%+v", p)
	}
	if p := b.PluginByAgent("agent.none"); p != nil {
		t.Fatalf("不存在的身份应返回 nil：%+v", p)
	}
}
