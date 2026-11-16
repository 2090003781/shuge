// Package main Platform Core 入口：配置驱动启动。
// 读取 config.yaml → 按插件清单拉起 builtin/external 插件 → 运行。
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"sync"
	"syscall"
	"time"

	"core/internal/api"
	"core/internal/bridge"
	"core/internal/config"
	"core/internal/core"
	"core/internal/ledger"
	"core/internal/logforward"
	"core/internal/logging"
	"core/internal/plugin"
	"core/internal/pluginauth"
	"core/internal/singlelock"
	"core/internal/supervisor"
	"core/internal/vault"
)

// defaultHTTPPort 内核（core）的监听端口 —— **内部端口**，只在本机回环上。
//
// 层级（2026-09-12 定案）：枢阁（主进程 core-host.exe）是软件本体，占 8790（唯一对外地址，
// 前门：内核在就反代到内核）；内核是它的附属进程，挪到 8795，任何外部调用方（DSH 插件、浏览器、
// 工具）都只认 8790 —— 由主进程转发，内核的端口不再出现在任何配置里。
const defaultHTTPPort = "8795"

// detachedProcess = DETACHED_PROCESS：不建控制台（拉起看门狗时用）
const detachedProcess = 0x00000008

// consoleCoreController 控制台顶部按钮的后端：停止（置暂停标志）/ 重启（安排看门狗接管）
type consoleCoreController struct {
	log    *slog.Logger
	stopCh chan struct{}
	once   *sync.Once
}

func (c *consoleCoreController) Stop(restart bool) {
	if restart {
		if exe, err := os.Executable(); err == nil {
			binDir := filepath.Dir(exe)
			helper := filepath.Join(binDir, "watchdog.exe")
			if _, err := os.Stat(helper); err != nil {
				c.log.Error("控制台重启失败：找不到看门狗", "path", helper)
			} else {
				cmd := exec.Command(helper, "--restart")
				cmd.Dir = filepath.Dir(binDir) // bin/.. = core 根目录
				cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: detachedProcess}
				if err := cmd.Start(); err != nil {
					c.log.Error("控制台重启失败：看门狗未能启动", "err", err)
				} else {
					_ = cmd.Process.Release()
					_ = os.Remove(api.PausedFlagPath()) // 重启 = 要继续跑
					c.log.Info("控制台请求重启：看门狗已接管拉起", "helper", helper)
				}
			}
		}
	} else {
		_ = os.MkdirAll("data", 0o755)
		_ = os.WriteFile(api.PausedFlagPath(), []byte(time.Now().Format(time.RFC3339)), 0o644)
		c.log.Info("控制台请求停止：已置暂停标志（看门狗不再自动拉起）")
	}
	c.once.Do(func() { close(c.stopCh) })
}

func main() {
	startedAt := time.Now()
	cfgPath := flag.String("config", "config.yaml", "配置文件路径")
	flag.Parse()

	// 日志自持（2026-09-11）：core 自己的日志落 data/logs/core.log（32MB 轮转、留 5 份），
	// 同时保留 stderr——原因见 internal/logging：detached 拉起时 stderr 可能是 NUL（曾 8 小时零日志）。
	logFile := logging.NewRotatingWriter(filepath.Join("data", "logs", "core.log"), 32<<20, 5)
	log := slog.New(slog.NewTextHandler(io.MultiWriter(logFile, logging.BestEffort(os.Stderr)), nil)) // 文件优先：stdio 不可用时也能落盘

	// 单实例锁（2026-09-12，真实事故后补）：账本是唯一真相源，绝不允许两个 core 同时写。
	// 2026-09-12 实测：控制台"重启"时旧实例 HTTP 已停、进程还在收尾，新实例就打开了账本，
	// 两边 seq 交叉（924/925 各写两次、校验器报 925→937 缺口）——静默数据损坏。
	// 抢不到锁 = 已有 core 在跑 → 立刻退出：不碰账本、不碰暂停标志（连 data/core.paused 都不删）。
	instLock, lkErr := singlelock.Acquire("core")
	if lkErr != nil {
		log.Error("已有 core 实例在运行，本次启动放弃（不触碰账本）", "err", lkErr)
		os.Exit(3)
	}
	defer instLock.Release()

	// owner 登记：记下"谁拉起的、什么时候、怎么拉起的"（进 data/owner.json + 账本）
	owner := currentOwner()
	writeOwnerFile(log, owner)
	defer markOwnerStopped()

	// core 已在跑 → "暂停标志"没有意义（它只用于阻止看门狗自动拉起），启动即清
	_ = os.Remove(api.PausedFlagPath())

	// 1. 加载配置
	cfg, err := config.Load(*cfgPath)
	if err != nil {
		log.Error("config load failed", "err", err)
		os.Exit(1)
	}
	log.Info("配置加载", "插件数", len(cfg.EnabledPlugins()))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// 2. 创建 Core（事件总线 + 注册表）
	c := core.New(core.Options{QueueLen: cfg.Bus.QueueLen, Logger: log})

	// 2.5 平台账本（唯一真相源）：先写后播——总线在扇出之前先把事件交给它
	// 账本目录/阈值支持环境变量覆盖 —— **演练专用**（生产用默认值即可）：
	//   CORE_LEDGER_DIR     账本目录（演练"账本打不开"：指到不存在的盘）
	//   CORE_LEDGER_MAX_MB  单文件轮转阈值（演练"轮转"：调小到 0.05MB 立刻触发）
	// 为什么留这两个旋钮：这两条路径平时根本不会跑到，只能靠演练验证；没有旋钮就只能改代码再改回来。
	ledOpts := ledger.Options{Dir: "data/ledger", Logger: log}
	if v := os.Getenv("CORE_LEDGER_DIR"); v != "" {
		ledOpts.Dir = v
		log.Warn("账本目录被环境变量覆盖（演练模式）", "dir", v)
	}
	if v := os.Getenv("CORE_LEDGER_MAX_MB"); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil && f > 0 {
			ledOpts.MaxFileBytes = int64(f * 1024 * 1024)
			log.Warn("账本轮转阈值被环境变量覆盖（演练模式）", "max_mb", f)
		}
	}
	led, lerr := ledger.Open(ledOpts)
	if lerr != nil {
		log.Error("账本打开失败（平台继续运行，但无账本）", "err", lerr)
	} else {
		c.Bus.SetAppender(led)
		defer led.Close()
		led.SetHooks(
			func(oldFile, newFile string, bytes int64) {
				_ = c.Bus.Publish(ctx, plugin.NewEvent("platform.ledger.rotate", "ledger",
					map[string]any{"old": oldFile, "new": newFile, "bytes": bytes}))
			},
			func(werr error, emergency string, backlog int) {
				_ = c.Bus.Publish(ctx, plugin.NewEvent("platform.ledger.write-error", "ledger",
					map[string]any{"err": werr.Error(), "emergency": emergency, "critical_backlog": backlog}))
			},
		)
		if info, ok := led.TakeOpenRotation(); ok {
			_ = c.Bus.Publish(ctx, plugin.NewEvent("platform.ledger.rotate", "ledger",
				map[string]any{"old": info.Old, "new": info.New, "bytes": info.Bytes, "at_open": true}))
		}
	}

	// 3. 全局事件打印（骨架期调试用：看到所有事件流转）
	_, _ = c.Bus.SubscribeNamed(ctx, "core.debug-print", nil, func(ctx context.Context, ev plugin.Event) error {
		fmt.Printf("  🔔 [event] %s <- %s\n", ev.Type, ev.Source)
		return nil
	})

	// 4. 按配置拉起插件
	sup := supervisor.New(log)
	sup.SetLogDir(filepath.Join("data", "logs")) // 输出目录（兜底直写用）
	// 插件面鉴权 token（2026-09-12 检修 P2）：读或生成 data/plugins/token.json，
	// 经 PLUGIN_TOKEN 注入给所有插件；插件面端点（register/poll/result）据此校验。
	pluginToken, tokErr := pluginauth.LoadOrCreate(".")
	if tokErr != nil {
		log.Warn("插件鉴权 token 不可用（插件面将不校验）", "err", tokErr)
	}
	sup.SetPluginEnv([]string{
		"PLUGIN_TOKEN=" + pluginToken,
		"CORE_URL=http://127.0.0.1:" + defaultHTTPPort,
		"CORE_DIR=" + mustAbs("."),
	})

	// 日志转发通道必须在**插件启动之前**挂上：否则插件首屏日志会走兜底（2026-09-12 实测）。
	// 但此刻 apiServer 还没创建 → 用一个懒解析的闭包：没装配就返回错误，转发器按"不可达"处理（兜底 + 标注）。
	var logForward logforward.Forwarder
	sup.SetLogForwarder(func(ctx context.Context, pluginID, tool string, args map[string]any, d time.Duration) (map[string]any, error) {
		if logForward == nil {
			return nil, fmt.Errorf("日志转发通道尚未装配（内核还在启动）")
		}
		return logForward(ctx, pluginID, tool, args, d)
	}, "plugin.log")
	// 日志记录已外移为独立插件（2026-09-12 检修步 3）：内核只负责把子进程的行**转发**给 plugin.log，
	// 由插件做统一行格式/分级/轮转/落盘；插件不可达时内核本地兜底直写（日志绝不丢）。
	// 进程监督事件进账本（supervisor 不依赖 eventbus，故用发布口注入）
	sup.SetPublisher(func(evType string, payload map[string]any) {
		_ = c.Bus.Publish(ctx, plugin.NewEvent(evType, "supervisor", payload))
	})
	started := 0

	// 跳过回收站/永久禁用名单中的插件（LoadSkipSet 读 trash.json + disabled.json）
	skip, err := api.LoadSkipSet()
	if err != nil {
		log.Error("load plugin skip set failed", "err", err)
		skip = &api.SkipSet{}
	}

	for _, pc := range cfg.EnabledPlugins() {
		if skip.Has(pc.ID) {
			log.Info("插件在回收站/禁用名单，跳过启动", "id", pc.ID)
			continue
		}
		switch pc.Type {
		case "builtin":
			// 内置插件位已清空（2026-09-12 检修）：平台不再把能力编进内核，能力一律走 external 插件进程
			log.Error("config 里有 builtin 插件，但内核已不再内置任何模块（全部走 external 插件）", "id", pc.ID)
			continue

		case "external":
			cmd := exec.Command(pc.Command, pc.Args...)
			if pc.WorkingDir != "" {
				cmd.Dir = pc.WorkingDir
			}
			cmd.Stdout = os.Stdout
			cmd.Stderr = os.Stderr
			policy := supervisor.DefaultPolicy()
			if pc.Supervisor != nil {
				if pc.Supervisor.MaxRestarts > 0 {
					policy.MaxRestarts = pc.Supervisor.MaxRestarts
				}
				if pc.Supervisor.RestartWaitMs > 0 {
					policy.RestartWait = time.Duration(pc.Supervisor.RestartWaitMs) * time.Millisecond
				}
			}
			if _, err := sup.Spawn(pc.ID, cmd, policy); err != nil {
				log.Error("external spawn failed", "id", pc.ID, "err", err)
				continue
			}
			log.Info("外部插件已启动", "id", pc.ID, "cmd", pc.Command)
			started++

		default:
			log.Error("未知插件类型", "id", pc.ID, "type", pc.Type)
		}
	}

	fmt.Printf("\n=== Platform Core 启动完成：%d 个插件 ===\n", started)

	// 5. 启动控制台 HTTP server（独立端口，纯 API）
	port := defaultHTTPPort
	if env := os.Getenv("CORE_PORT"); env != "" {
		port = env
	}
	// 挂 Vault 资源（config.resources.vault.path）
	var vaultRes *vault.Vault
	if vp, ok := cfg.Resources["vault"].(map[string]any); ok {
		if path, ok := vp["path"].(string); ok {
			if v, err := vault.New(path); err == nil {
				vaultRes = v
				log.Info("Vault 资源已挂载", "path", path)
			} else {
				log.Error("Vault 挂载失败", "err", err)
			}
		}
	}
	apiServer := api.New(c.Bus, c.Registry, sup, vaultRes, "web", log)

	// 控制台顶部「启停」按钮：停止 → 优雅停机 + 置暂停标志；重启 → 看门狗接管拉起
	stopCh := make(chan struct{})
	stopOnce := &sync.Once{}
	apiServer.SetCoreController(&consoleCoreController{log: log, stopCh: stopCh, once: stopOnce})

	// 5.5 中间平台（bridge 主框架）：骨架期注册 reasonix agent 并启动（本地闭环验证）
	var br *bridge.Bridge
	if cfg.Bridge.Enabled {
		br = bridge.New(bridge.Options{Bus: c.Bus, Logger: log})
		// reasonix 通道已外移为独立插件（2026-09-12 检修步 4）：plugin.reasonix 注册时声明
		// agent_id=agent.reasonix，平台据此登记"插件背书的 agent"（身份与 inbox 投递面保持不变）。
		// DSH（DeepSeek Harness）agent 适配器（2026-09-11 接入）：
		// 身份 = agent.dsh；投递面 = data/inbox/dsh（DSH 侧 dsh-second-brain 插件取件）。
		// DSH → 平台方向走 /api/bridge/send 与 /api/agent/session-start，无需额外接线。
		// DSH 通道已外移为独立插件（2026-09-12 检修步 1）：plugin.dsh 注册时声明 agent_id=agent.dsh，
		// 平台据此登记"插件背书的 agent"（见 bridge.RegisterExternalAgent），身份与投递面保持不变。
		// 功能模块：笔记查找（账本已独立为 internal/ledger，不再是模块）
		// 账本查询/状态/校验注入 API（/api/events/search 兼容保留）
		if led != nil {
			apiServer.SetLedger(led)
			apiServer.SetLogDir(filepath.Join("data", "logs"))
			apiServer.RegisterEventSearcher(ledger.Searcher{Dir: led.Dir()})
		}
		// 笔记查找已外移为独立插件（2026-09-12 检修步 5）：module.note-search 不再是内核模块，
		// 改为 plugin.note-search（capabilities=["note.search"]），内核按能力路由工具调用过去。
		apiServer.RegisterBridge(br)
		apiServer.SetPluginToken(pluginToken)
		// agent 上报面鉴权模式（config.security.agent_uplink_auth：soft/strict/off）
		apiServer.SetAgentAuthMode(cfg.Security.AgentUplinkAuth)
		// 插件调用通道：插件背书的 agent 用它做定向投递（DSH 通道就是第一个用户）
		br.SetPluginCaller(apiServer)
		// 日志转发通道：supervisor 把子进程输出交给日志插件（同一个调用通道，不经过网络栈）
		logForward = apiServer.CallPlugin
		if err := br.Start(ctx); err != nil {
			log.Error("bridge start failed", "err", err)
		}
		log.Info("中间平台已启动", "agents", len(br.Agents()), "modules", len(br.Modules()))
		// 接入留痕：agent.registered（在 bridge/module 启动之后发，确保日志模块已订阅总线）
		// 身份现在挂在插件上（辞玖 2026-09-12 定），所以留痕也按"插件声明的身份"发。
		for _, a := range br.Agents() { // 内核内 agent（当前无）
			_ = c.Bus.Publish(ctx, plugin.NewEventScoped("platform.agent.registered", "agent-interface",
				map[string]any{"id": a.ID, "name": a.Name, "capabilities": a.Capabilities},
				plugin.ScopeAgent, plugin.ScopePlatform))
		}
		for _, p := range br.Plugins() { // 外部插件声明的身份（agentId）
			if p.AgentID == "" {
				continue
			}
			_ = c.Bus.Publish(ctx, plugin.NewEventScoped("platform.agent.registered", "agent-interface",
				map[string]any{"id": p.AgentID, "name": p.AgentName, "capabilities": p.AgentCapabilities,
					"provided_by": p.ID},
				plugin.ScopeAgent, plugin.ScopePlatform))
		}
	}

	// 5.6 插件运行时管理器（会话级 stop/start/delete）
	apiServer.SetPluginManager(api.NewPluginManager(ctx, log, c.Bus, c.Registry, sup,
		c.Env, cfg.EnabledPlugins()))
	// 插件清单路径：/api/plugins/reload（热加）要按**同一个文件**重读，别各写一份默认值
	apiServer.SetPluginConfigPath(*cfgPath)

	// 5.7 上下文包管理（注入开关 + 直读包；包由 plugin.context 按路由生成）
	ctxDir := cfg.Agent.SessionStart.ContextDir
	ctxRoutes := make([]api.ContextRoute, 0, len(cfg.Agent.SessionStart.Routes))
	for _, r := range cfg.Agent.SessionStart.Routes {
		ctxRoutes = append(ctxRoutes, api.ContextRoute{Agent: r.Agent, Enabled: r.Enabled, Docs: r.Docs})
	}
	apiServer.SetContextPacks(api.NewContextPackManager(ctxDir, ctxRoutes))
	log.Info("上下文包管理已装配", "dir", ctxDir, "routes", len(ctxRoutes))

	// 5.9 心跳点名（probe）：对每个 external 插件按周期点名——活性 + 功能 + 状态快照一次拿全
	probeTargets := make([]api.ProbeTarget, 0, len(cfg.Plugins))
	for _, pc := range cfg.EnabledPlugins() {
		if pc.Type != "external" || skip.Has(pc.ID) {
			continue
		}
		probeTargets = append(probeTargets, api.ProbeTargetFromConfig(pc)) // 与热加共用同一份规则
	}
	apiServer.StartProbe(probeTargets)

	// 先在主 goroutine 里**绑定端口**：绑不上就立刻退出（2026-09-20 审计发现的老写法的问题 ——
	// 原来 ListenAndServe 放在 goroutine 里、失败只记一行日志，于是这个 core 会带着
	// 单实例锁与已打开的账本继续活着，而对外服务的是另一个实例 → 跨会话双写账本的真实路径）。
	ln, lnErr := net.Listen("tcp", "127.0.0.1:"+port)
	if lnErr != nil {
		log.Error("内核端口绑定失败 → 立刻退出（避免与别的内核实例抢同一份账本）", "addr", "127.0.0.1:"+port, "err", lnErr)
		os.Exit(7)
	}
	httpSrv := &http.Server{Handler: apiServer.Handler()}
	go func() {
		log.Info("控制台 HTTP 服务启动", "url", "http://127.0.0.1:"+port)
		if err := httpSrv.Serve(ln); err != nil && err != http.ErrServerClosed {
			log.Error("http server error", "err", err)
		}
	}()

	// 5.8 启动留痕：平台自身事件进账本（这是"平台自己"第一次有账）
	_ = c.Bus.Publish(ctx, plugin.NewEvent("platform.core.started", "core", map[string]any{
		"pid": os.Getpid(), "plugins": started, "routes": len(ctxRoutes), "ledger": led != nil,
		"launched_by": owner.LaunchedBy, // 归属：谁把这个 core 拉起来的
	}))
	if led != nil {
		if st := led.Stats(); st.TruncatedBytes > 0 {
			_ = c.Bus.Publish(ctx, plugin.NewEvent("platform.ledger.recovered", "ledger", map[string]any{
				"truncated_bytes": st.TruncatedBytes, "last_seq": st.LastSeq,
			}))
		}
	}

	// 6. 演示事件发布（模拟外部触发）
	time.Sleep(300 * time.Millisecond)
	_ = c.Bus.Publish(ctx, plugin.NewEvent("note.saved", "console", map[string]any{"title": "配置驱动启动测试"}))

	// 7. 等待退出信号
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	select {
	case s := <-sig:
		log.Info("收到退出信号", "signal", s.String())
	case <-stopCh:
		log.Info("收到控制台停机请求")
	}

	fmt.Println("\n=== 收到退出信号，优雅停止 ===")
	_ = c.Bus.Publish(ctx, plugin.NewEvent("platform.core.stopping", "core", map[string]any{"reason": "signal"}))
	// 给 HTTP 一个短预算：浏览器挂着的 SSE 长连接会让无超时的 Shutdown 一直等下去（2026-09-11 实测：
	// stopping 入了账、stopped 却因为卡在这里而没落上）
	shutCtx, shutCancel := context.WithTimeout(context.Background(), 2*time.Second)
	_ = httpSrv.Shutdown(shutCtx)
	shutCancel()
	if br != nil {
		br.Stop(ctx)
	}
	sup.StopAll()
	sup.CloseJob() // 关掉作业对象：漏网插件进程由内核立即回收（无孤儿）
	c.Shutdown(ctx)
	_ = c.Bus.Publish(ctx, plugin.NewEvent("platform.core.stopped", "core", map[string]any{
		"uptime_s": int64(time.Since(startedAt).Seconds()),
	}))
	if led != nil {
		led.Flush()
	}
	log.Info("平台已停止")
}

// mustAbs 把相对路径转绝对（失败就原样返回：插件读不到时自己会报错，不必在这里炸）
func mustAbs(p string) string {
	a, err := filepath.Abs(p)
	if err != nil {
		return p
	}
	return a
}
