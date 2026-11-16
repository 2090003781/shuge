// 本文件：事件目录（catalog）——"哪类事件属于谁、算哪一档"的唯一真相。
//
// 为什么要有目录（2026-09-11 辞玖拍板）：
//
//	· 优先级（落盘档位）是会变的运行时策略，不编进事件名，只在这里定义一次
//	· 对象域（realm）同样进字段，不进名字——名字保持 ASCII 点分且稳定
//	· 命名规范：`platform.<子系统>.<动作>`（机器名，稳定）；展示名用中文（Desc）
//	· 任何以 `platform.` 开头但不在本目录里的事件 = 漏登记，会被计入 unknown 计数（可见）
package ledger

import "strings"

// Class 落盘档位：决定写入路径与能否丢弃
type Class string

const (
	// ClassCritical 同步档：等本批 fsync 落盘后才返回；任何情况下不丢
	ClassCritical Class = "critical"
	// ClassBusiness 批量档：随组提交落盘；写不动时可按阶梯降级丢弃，但必须留痕
	ClassBusiness Class = "business"
	// ClassTelemetry 遥测档：可采样、可丢（本期不收录外部域事件）
	ClassTelemetry Class = "telemetry"
)

// 对象域（realm）：这条事件"是谁的事"
const (
	RealmPlatform = "platform" // 平台自身（本期唯一入账的域）
	RealmAgent    = "agent"    // agent 的活动（将来由 agent 日志插件消费账本生成）
	RealmPlugin   = "plugin"   // external 插件的活动
	RealmVault    = "vault"    // 笔记库变更
	RealmExternal = "external" // 未归类的外部事件
)

// Spec 目录条目
type Spec struct {
	Realm  string
	Class  Class
	Desc   string   // 展示名（中文，控制台用）
	Fields []string // 关键字段（文档 + 校验用）
}

// PlatformPrefix 平台域事件前缀：以此为界的类型都属于"平台自身"
const PlatformPrefix = "platform."

// Catalog 事件目录 v0：平台自身事件全集。
// 新增平台事件时必须在此登记，否则会被 unknown 计数抓到（漏登记可见化）。
var Catalog = map[string]Spec{
	// ── core 生命周期 ──────────────────────────────────────────
	"platform.core.started":   {RealmPlatform, ClassCritical, "核心启动", []string{"pid", "plugins", "routes"}},
	"platform.core.stopping":  {RealmPlatform, ClassCritical, "核心停止中", []string{"reason"}},
	"platform.core.stopped":   {RealmPlatform, ClassCritical, "核心已停止", []string{"uptime_s"}},
	"platform.core.recovered": {RealmPlatform, ClassCritical, "核心启动恢复", []string{"truncated_bytes", "last_seq"}},

	// ── 账本自身 ───────────────────────────────────────────────
	"platform.ledger.rotate":        {RealmPlatform, ClassCritical, "账本轮转", []string{"file", "bytes"}},
	"platform.ledger.recovered":     {RealmPlatform, ClassCritical, "账本恢复（截断半行）", []string{"file", "truncated_bytes", "last_seq"}},
	"platform.ledger.write-error":   {RealmPlatform, ClassCritical, "账本写盘失败", []string{"err", "emergency", "pending"}},
	"platform.ledger.degraded":      {RealmPlatform, ClassCritical, "账本降级（按阶梯丢弃）", []string{"class", "dropped"}},
	"platform.ledger.unknown-type":  {RealmPlatform, ClassBusiness, "未登记的平台事件类型", []string{"type", "count"}},
	"platform.ledger.verify-failed": {RealmPlatform, ClassCritical, "账本校验失败", []string{"gaps", "bad_lines"}},

	// ── 进程监督（supervisor）──────────────────────────────────
	"platform.process.started":        {RealmPlatform, ClassCritical, "插件进程启动", []string{"id", "pid"}},
	"platform.process.exited":         {RealmPlatform, ClassCritical, "插件进程退出", []string{"id", "pid", "code", "uptime_s", "restarts"}},
	"platform.process.restarting":     {RealmPlatform, ClassCritical, "插件进程重启中", []string{"id", "attempt", "wait_ms"}},
	"platform.process.gave-up":        {RealmPlatform, ClassCritical, "插件重启放弃（熔断）", []string{"id", "restarts", "last_code"}},
	"platform.process.kill-timeout":   {RealmPlatform, ClassCritical, "插件停止超时", []string{"id", "timeout_ms"}},
	"platform.process.stop-requested": {RealmPlatform, ClassBusiness, "插件主动停止请求", []string{"id", "by"}},
	"platform.process.restart-failed": {RealmPlatform, ClassCritical, "插件重启失败", []string{"id", "err"}},

	// ── 总线健康 ───────────────────────────────────────────────
	"platform.bus.dropped":            {RealmPlatform, ClassCritical, "订阅者队列满丢弃事件", []string{"subscriber", "type", "count"}},
	"platform.bus.handler.error":      {RealmPlatform, ClassCritical, "订阅者处理出错", []string{"subscriber", "type", "err"}},
	"platform.bus.handler.panic":      {RealmPlatform, ClassCritical, "订阅者 panic（已隔离）", []string{"subscriber", "type"}},
	"platform.bus.subscriber.added":   {RealmPlatform, ClassBusiness, "订阅者登记", []string{"name"}},
	"platform.bus.subscriber.removed": {RealmPlatform, ClassBusiness, "订阅者注销", []string{"name"}},

	// ── 入站可见性（今晚黑洞的探针）────────────────────────────
	"platform.bridge.recv-summary": {RealmPlatform, ClassBusiness, "入站事件汇总（按来源）", []string{"window_s", "by_source", "total"}},

	// ── 组件装配 ───────────────────────────────────────────────
	"platform.plugin.registered":     {RealmPlatform, ClassBusiness, "插件注册", []string{"id", "name", "capabilities"}},
	"platform.plugin.re-registered":  {RealmPlatform, ClassBusiness, "插件重注册（幂等）", []string{"id"}},
	"platform.plugin.started":        {RealmPlatform, ClassBusiness, "插件启用", []string{"id"}},
	"platform.plugin.stopped":        {RealmPlatform, ClassBusiness, "插件停用", []string{"id"}},
	"platform.plugin.deleted":        {RealmPlatform, ClassBusiness, "插件移除", []string{"id"}},
	"platform.module.registered":     {RealmPlatform, ClassBusiness, "模块挂载", []string{"id", "name"}},
	"platform.module.started":        {RealmPlatform, ClassBusiness, "模块启动", []string{"id"}},
	"platform.module.stopped":        {RealmPlatform, ClassBusiness, "模块停止", []string{"id"}},
	"platform.agent.registered":      {RealmPlatform, ClassBusiness, "agent 接入登记", []string{"id", "name"}},
	"platform.agent.session.started": {RealmPlatform, ClassBusiness, "agent 会话开始（含注入结果）", []string{"agent", "sessionId", "injected", "reason", "bytes"}},

	// ── 心跳点名（活性）────────────────────────────────────────
	"platform.plugin.suspect":   {RealmPlatform, ClassCritical, "插件点名失败（疑似卡死）", []string{"plugin", "fails", "last_error"}},
	"platform.plugin.recovered": {RealmPlatform, ClassCritical, "插件点名恢复", []string{"plugin", "from"}},
	"platform.plugin.dead":      {RealmPlatform, ClassCritical, "插件判定死亡（拉起无效）", []string{"plugin", "fails", "last_error"}},

	// ── 动作模型（总账记状态，详情走分账 ref）──────────────────
	"platform.action.started": {RealmPlatform, ClassBusiness, "动作发起", []string{"action.id", "action.name", "action.expect_within_ms"}},
	"platform.action.settled": {RealmPlatform, ClassBusiness, "动作结束", []string{"action.id", "outcome.status", "outcome.duration_ms", "ref.owner"}},
	"platform.action.timeout": {RealmPlatform, ClassCritical, "动作超时（有发起无结束）", []string{"action.id", "expect_within_ms"}},

	// ── 平台行为留痕 ───────────────────────────────────────────
	"platform.inject.sent":   {RealmPlatform, ClassBusiness, "定向推送留痕", []string{"target", "delivered", "eventType"}},
	"platform.inject.failed": {RealmPlatform, ClassBusiness, "定向推送失败", []string{"target", "failed", "eventType"}},

	// ── 配置与路由 ─────────────────────────────────────────────
	"platform.config.loaded":        {RealmPlatform, ClassBusiness, "配置加载", []string{"path", "plugins", "routes"}},
	"platform.config.route-changed": {RealmPlatform, ClassBusiness, "注入路由开关变更", []string{"agent", "enabled"}},

	// ── 上下文注入链路（包生产 → 交付 → agent 收讫，2026-09-13 加）──
	// 三段各有主：pack-ready=插件组完包；injected=agent 真把包放进模型上下文（跨进程回执）；
	// pack-changed=命名包被新建/删除/改（控制台动作，要能追到是谁在什么时候动了什么）。
	"platform.context.pack-ready":    {RealmPlatform, ClassBusiness, "上下文包已生成", []string{"packs", "reason", "dir"}},
	"platform.context.pack-changed":  {RealmPlatform, ClassBusiness, "命名包变更（新建/删除/改）", []string{"op", "pack_id", "name", "files"}},
	"platform.context.injected":      {RealmPlatform, ClassBusiness, "agent 确认收到上下文包", []string{"agent", "sessionId", "bytes"}},
	"platform.context.inject-missed": {RealmPlatform, ClassBusiness, "取了包但没注入（半截链路）", []string{"agent", "sessionId", "reason"}},
	// ── 常驻巡检（主进程每 5 分钟自查，只记"跃迁"，2026-09-13 补登记）──
	// 为什么现在才登记：巡检只有在"结论跃迁"时才写账（problem/recovered/changed），
	// 一直没跃迁过 → 账本里一条 unknown 都没有 → 漏登记被完美掩盖。补上。
	"platform.patrol.problem":   {RealmPlatform, ClassCritical, "巡检发现问题（跃迁）", []string{"kind", "summary", "fail", "warn", "problems"}},
	"platform.patrol.recovered": {RealmPlatform, ClassCritical, "巡检恢复（跃迁）", []string{"kind", "summary", "fail", "warn"}},
	"platform.patrol.changed":   {RealmPlatform, ClassBusiness, "巡检结论变化（跃迁）", []string{"kind", "summary", "known"}},
	// ── 上报面鉴权（谁在没带 token 敲门，2026-09-13 补登记）─────
	// 同一路径每分钟最多一条（节流在 internal/api/corectl.go），所以量可控、可以按 critical 收。
	"platform.auth.missing": {RealmPlatform, ClassCritical, "上报面鉴权缺失/失败", []string{"path", "mode", "client", "method", "ua", "origin"}},

	// ── 已登记但本期不入账的外部域（占位：将来 agent 日志插件用）──
	"agent.tool-call":   {RealmAgent, ClassBusiness, "agent 工具调用", []string{"agent", "tool", "ok"}},
	"vault.note-saved":  {RealmVault, ClassBusiness, "笔记保存", []string{"title"}},
	"plugin.clock.tick": {RealmPlugin, ClassTelemetry, "时钟心跳", []string{"seq", "local"}},
	// 注：原 `plugin.context.pack` 条目已废（插件实际发的是 context.pack-ready，名字从未对上）；
	// 现由 platform.context.pack-ready / pack-changed 取代。
}

// Classify 判定一个事件类型的域与档位。
// 返回 (spec, known)：known=false 表示不在目录里——
//
//	· 以 `platform.` 开头 → 域仍算平台（属于漏登记，会被计入 unknown 计数）
//	· 其余一律按外部域处理（本期不入账，也不算漏登记）
func Classify(evType string) (Spec, bool) {
	if s, ok := Catalog[evType]; ok {
		return s, true
	}
	if strings.HasPrefix(evType, PlatformPrefix) {
		return Spec{Realm: RealmPlatform, Class: ClassBusiness, Desc: "（未登记的平台事件类型）"}, false
	}
	return Spec{Realm: RealmExternal, Class: ClassTelemetry, Desc: "（外部事件，本期不入账）"}, false
}
