// Package supervisor 进程监督器：管理插件子进程的启动/监控/崩溃重启/资源限额。
// 实现 AD-1（监督式多进程）与 NFR-1（局部不影响全局、可恢复）。
package supervisor

import (
	"errors"
	"log/slog"
	"os"
	"os/exec"
	"sync"
	"time"

	"core/internal/logforward"
)

// Policy 单插件的监督策略
type Policy struct {
	MaxRestarts int           // 连续崩溃最大重启次数（0=不限）
	RestartWait time.Duration // 重启前等待
	KillTimeout time.Duration // 停止超时后强杀
	MaxMemoryMB int64         // 内存上限（0=不限）
}

// DefaultPolicy 默认策略
func DefaultPolicy() Policy {
	return Policy{
		MaxRestarts: 5,
		RestartWait: time.Second,
		KillTimeout: 5 * time.Second,
		MaxMemoryMB: 0,
	}
}

// ManagedProcess 被监督的插件进程
type ManagedProcess struct {
	ID          string
	Cmd         *exec.Cmd
	policy      Policy
	log         *slog.Logger
	mu          sync.Mutex
	restarts    int
	stopped     bool // 主动停止标记（主动停不重启）
	done        chan error
	sup         *Supervisor          // 回指监督器（发布平台事件用）
	startedAt   time.Time            // 本实例启动时刻（算存活时长）
	sink        *logforward.Sink     // 日志转发器：行原样交给日志插件，插件不可达时本地兜底直写
	logFwd      logforward.Forwarder // 转发通道（api 层注入；nil = 直接本地兜底）
	logPluginID string               // 日志插件 id（默认 plugin.log）
}

// Supervisor 监督器（进程树）
type Supervisor struct {
	mu          sync.Mutex
	procs       map[string]*ManagedProcess
	log         *slog.Logger
	pub         func(evType string, payload map[string]any) // 平台事件发布口（main 注入）
	logDir      string                                      // 统一输出目录（main 注入；空 = 不接管道）
	logFwd      logforward.Forwarder                        // 日志转发通道（api 层注入；nil = 直接本地兜底）
	logPluginID string                                      // 日志插件 id（默认 plugin.log）
	pluginEnv   []string                                    // 注入给所有插件的环境变量（PLUGIN_TOKEN / CORE_URL / CORE_DIR）
	job         *jobObject                                  // Windows 作业对象：core 死 → 整棵树被内核回收（无孤儿）
}

// SetLogForwarder 注入"平台 → 日志插件"的转发通道（api 层 CallPlugin 实现）。
// 没注入也能跑：转发器会自动走本地兜底直写（日志绝不丢）。
func (s *Supervisor) SetLogForwarder(f logforward.Forwarder, pluginID string) {
	s.mu.Lock()
	s.logFwd = f
	if pluginID != "" {
		s.logPluginID = pluginID
	}
	s.mu.Unlock()
}

// SetLogDir 注入统一输出目录：从此子进程不再拥有控制台，
// stdout/stderr 全部经 logsink 收进 data/logs/<source>.log（2026-09-11 辞玖定）
func (s *Supervisor) SetLogDir(dir string) {
	s.mu.Lock()
	s.logDir = dir
	s.mu.Unlock()
}

// New 创建监督器
func New(log *slog.Logger) *Supervisor {
	if log == nil {
		log = slog.Default()
	}
	s := &Supervisor{procs: make(map[string]*ManagedProcess), log: log}
	// 作业对象（KILL_ON_JOB_CLOSE）：创建失败不致命，退化回插件侧"父进程看护"兜底
	if j, err := newJobObject(); err == nil {
		s.job = j
	} else {
		log.Warn("supervisor: 作业对象创建失败（退化用父进程看护兜底）", "err", err)
	}
	return s
}

// CloseJob 关闭作业对象 —— 作业内仍存活的进程会被内核立即终止（"整树回收"的收口动作）
func (s *Supervisor) CloseJob() {
	s.mu.Lock()
	j := s.job
	s.job = nil
	s.mu.Unlock()
	if j != nil {
		if err := j.Close(); err != nil && s.log != nil {
			s.log.Warn("supervisor: 关闭作业对象失败", "err", err)
		}
	}
}

// SetPublisher 注入事件发布口（main 装配 bus 时调用；supervisor 不依赖 eventbus 包）
func (s *Supervisor) SetPublisher(p func(evType string, payload map[string]any)) {
	s.mu.Lock()
	s.pub = p
	s.mu.Unlock()
}

// emit 发布平台自身事件（未注入发布口时静默丢弃，不影响监督逻辑）
func (s *Supervisor) emit(evType string, payload map[string]any) {
	s.mu.Lock()
	p := s.pub
	s.mu.Unlock()
	if p != nil {
		p(evType, payload)
	}
}

// Spawn 启动并监督一个插件进程。cmd 就绪后调用；若进程退出且非主动停止 → 按策略重启。
// SetPluginEnv 设置注入给所有插件进程的环境变量（两条 spawn 路径——main 启动循环与
// 控制台的插件管理器——都经过 Spawn，所以在这里统一生效）。
func (s *Supervisor) SetPluginEnv(env []string) {
	s.mu.Lock()
	s.pluginEnv = env
	s.mu.Unlock()
}

func (s *Supervisor) Spawn(id string, cmd *exec.Cmd, policy Policy) (*ManagedProcess, error) {
	s.mu.Lock()
	pluginEnv := append([]string{}, s.pluginEnv...)
	if _, exists := s.procs[id]; exists {
		s.mu.Unlock()
		return nil, errors.New("process already managed: " + id)
	}
	mp := &ManagedProcess{ID: id, Cmd: cmd, policy: policy, log: s.log, done: make(chan error, 1), sup: s}
	s.procs[id] = mp
	logDir := s.logDir
	s.mu.Unlock()

	// 插件环境：CORE_URL/PLUGIN_TOKEN 等由内核统一注入（插件据此回连与鉴权）
	if len(pluginEnv) > 0 {
		cmd.Env = append(os.Environ(), pluginEnv...)
	}
	// 统一输出中枢：把子进程的 stdout/stderr 收进平台（子进程因此不需要控制台）
	// 交出去的是 io.Writer（不是 *os.File）→ Go 每次启动新建管道，**不存在继承旧句柄的问题**
	if logDir != "" {
		mp.sink = logforward.New(logDir, id, 32<<20, 5, s.logFwd, s.logPluginID)
		cmd.Stdout = mp.sink
		cmd.Stderr = mp.sink
		cmd.Stdin = nil
	}

	if err := mp.start(); err != nil {
		s.mu.Lock()
		delete(s.procs, id)
		s.mu.Unlock()
		return nil, err
	}
	go s.watch(mp)
	return mp, nil
}

// Stop 主动停止（不触发重启）
func (s *Supervisor) Stop(id string) error {
	s.mu.Lock()
	mp, ok := s.procs[id]
	s.mu.Unlock()
	if !ok {
		return errors.New("process not managed: " + id)
	}
	mp.mu.Lock()
	mp.stopped = true
	mp.mu.Unlock()
	s.emit("platform.process.stop-requested", map[string]any{"id": id, "by": "supervisor.Stop"})
	mp.stop()
	s.mu.Lock()
	delete(s.procs, id)
	s.mu.Unlock()
	return nil
}

// Restart 主动重启一个被托管的进程（供心跳点名判定"卡死"后自愈使用）。
// 与崩溃重启的区别：**不计入 MaxRestarts 崩溃预算**（人为判定卡死 ≠ 崩溃循环）。
// 实现上只是 Kill 掉进程——随后的拉起由 watch() 按既有崩溃重启路径完成（并发出 started 事件）。
func (s *Supervisor) Restart(id string) error {
	s.mu.Lock()
	mp, ok := s.procs[id]
	s.mu.Unlock()
	if !ok {
		return errors.New("process not managed: " + id)
	}
	mp.mu.Lock()
	if mp.Cmd == nil || mp.Cmd.Process == nil {
		mp.mu.Unlock()
		return errors.New("process has no handle: " + id)
	}
	proc := mp.Cmd.Process
	mp.restarts = 0
	mp.mu.Unlock()
	s.emit("platform.process.stop-requested", map[string]any{"id": id, "by": "probe.restart"})
	return proc.Kill()
}

// StopAll 停止全部
func (s *Supervisor) StopAll() {
	s.mu.Lock()
	ids := make([]string, 0, len(s.procs))
	for id := range s.procs {
		ids = append(ids, id)
	}
	s.mu.Unlock()
	for _, id := range ids {
		_ = s.Stop(id)
	}
}

// Status 全部进程状态（控制台监控用）
func (s *Supervisor) Status() []map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]map[string]any, 0, len(s.procs))
	for id, mp := range s.procs {
		mp.mu.Lock()
		out = append(out, map[string]any{
			"id":       id,
			"running":  mp.Cmd.Process != nil && mp.Cmd.ProcessState == nil,
			"restarts": mp.restarts,
			"stopped":  mp.stopped,
		})
		mp.mu.Unlock()
	}
	return out
}

// ---- 内部 ----

func (mp *ManagedProcess) start() error {
	// 启动前统一设置子进程创建标志（隐藏控制台窗口，见 cmd_windows.go）。
	// 放在这里可同时覆盖两条路径：Spawn 首次启动、watch 崩溃重启
	// （重启时 Cmd 由 cloneCmd 重建，标志需要重新设置）。
	prepareCmd(mp.Cmd)
	if err := mp.Cmd.Start(); err != nil {
		return err
	}
	mp.mu.Lock()
	mp.startedAt = time.Now()
	mp.mu.Unlock()
	// 入作业对象：core 一死，本进程随之被内核回收（不再依赖插件自觉的父进程看护）
	if mp.sup != nil && mp.sup.job != nil {
		if err := mp.sup.job.assign(mp.Cmd.Process); err != nil {
			mp.log.Warn("supervisor: 加入作业对象失败（退化用父进程看护兜底）", "id", mp.ID, "err", err)
		}
	}
	mp.log.Info("supervisor: started", "id", mp.ID, "pid", mp.Cmd.Process.Pid)
	if mp.sup != nil {
		mp.sup.emit("platform.process.started", map[string]any{"id": mp.ID, "pid": mp.Cmd.Process.Pid})
	}
	return nil
}

func (mp *ManagedProcess) stop() {
	if mp.Cmd.Process == nil {
		return
	}
	done := make(chan struct{})
	go func() {
		_ = mp.Cmd.Process.Kill()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(mp.policy.KillTimeout):
		mp.log.Warn("supervisor: kill timeout, process may linger", "id", mp.ID)
		if mp.sup != nil {
			mp.sup.emit("platform.process.kill-timeout", map[string]any{"id": mp.ID, "timeout_ms": mp.policy.KillTimeout.Milliseconds()})
		}
	}
	_ = mp.Cmd.Wait()
	mp.mu.Lock()
	if mp.sink != nil {
		_ = mp.sink.Close()
		mp.sink = nil
	}
	mp.mu.Unlock()
}

// watch 监视进程：退出时判断是否重启
func (s *Supervisor) watch(mp *ManagedProcess) {
	err := mp.Cmd.Wait()
	mp.mu.Lock()
	stopped := mp.stopped
	mp.mu.Unlock()
	if stopped {
		s.log.Info("supervisor: stopped", "id", mp.ID)
		return
	}
	s.log.Warn("supervisor: process exited", "id", mp.ID, "err", err)
	mp.mu.Lock()
	uptime := time.Since(mp.startedAt)
	mp.mu.Unlock()
	s.emit("platform.process.exited", map[string]any{
		"id": mp.ID, "code": exitCodeOf(err),
		"uptime_s": int64(uptime.Seconds()), "err": errStringOf(err),
	})

	mp.mu.Lock()
	mp.restarts++
	restarts := mp.restarts
	mp.mu.Unlock()

	if mp.policy.MaxRestarts > 0 && restarts > mp.policy.MaxRestarts {
		s.log.Error("supervisor: restart limit reached, giving up", "id", mp.ID, "restarts", restarts)
		s.emit("platform.process.gave-up", map[string]any{"id": mp.ID, "restarts": restarts, "last_err": errStringOf(err)})
		s.mu.Lock()
		delete(s.procs, mp.ID)
		s.mu.Unlock()
		return
	}

	s.emit("platform.process.restarting", map[string]any{
		"id": mp.ID, "attempt": restarts, "wait_ms": mp.policy.RestartWait.Milliseconds(),
	})
	time.Sleep(mp.policy.RestartWait)
	mp.mu.Lock()
	mp.Cmd = cloneCmd(mp.Cmd) // 重新 exec
	mp.mu.Unlock()
	if err := mp.start(); err != nil {
		s.log.Error("supervisor: restart failed", "id", mp.ID, "err", err)
		s.emit("platform.process.restart-failed", map[string]any{"id": mp.ID, "err": errStringOf(err)})
		return
	}
	go s.watch(mp)
}

// cloneCmd 复制 exec.Cmd（exec.Cmd 不能复用，需重建）。
// 注意 Stdout/Stderr 复制的是 logsink 的 io.Writer——重启时 Go 会新建管道重新拷贝，
// 因此不会出现"继承上一个实例的陈旧句柄"的问题（2026-09-11 起）。
func cloneCmd(c *exec.Cmd) *exec.Cmd {
	nc := exec.Command(c.Path, c.Args[1:]...)
	nc.Dir = c.Dir
	nc.Env = c.Env
	nc.Stdout = c.Stdout
	nc.Stderr = c.Stderr
	nc.Stdin = c.Stdin
	return nc
}

// exitCodeOf 取退出码（非 ExitError 返回 -1）
func exitCodeOf(err error) int {
	if err == nil {
		return 0
	}
	var ee *exec.ExitError
	if errors.As(err, &ee) {
		return ee.ExitCode()
	}
	return -1
}

// errStringOf 错误摘要（空错误返回空串）
func errStringOf(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}
