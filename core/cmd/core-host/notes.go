//go:build windows

// 本文件：**库（ai-notes / 笔记软件主体）** 的守护。
//
// 层级（2026-09-14 辞玖纠正）：
//
//	库   = 主体：界面与数据都在它身上   ← 宿主**先**保它
//	内核 = 附属：总线 / 账本 / 插件 / agent 桥   ← 宿主后保它
//
// 依据：内核停着，库照样读写检索（已实测）；库停了，笔记就没了。
// 所以**巡检顺序不能反** —— 曾经把"库交给内核托管"的思路是反的（附属管主体）。
//
// 与内核的差别：内核是可被启停的附属（单实例锁 / 暂停标志 / 熔断），
// 库是主体，**没有启停这回事**（2026-09-20 辞玖："库不需要启停的说法，永远保持在线"）——
// 宿主只做"探测 → 不在就拉起"，不再有 notes.paused 那套"人工停止"语义。
package main

import (
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

var (
	notesSpawnMu   sync.Mutex
	lastNotesSpawn time.Time

	// notesHoldUntil 维护窗口：只有 /api/notes/stop（维护入口）与"关闭枢阁"会用它。
	// 它不会让库"停用"，只是推迟几十秒再拉回（库永远在线）。
	notesHoldMu    sync.Mutex
	notesHoldUntil time.Time

	// notesShortLived 连续"起不来"的次数（存活不到 10 秒即算一次）：
	// 一次两次立刻重拉（正常被杀的库就属于这种），连续 3 次才退避 —— 那才是崩溃循环。
	notesShortLivedMu sync.Mutex
	notesShortLived   int
)

// noteExitLifetime 记一次库进程的"寿命"，返回要不要退避：
// 活够 10 秒 = 正常意外 → 立刻重拉（返回 0）；
// 活不到 10 秒 → 计数；连续 3 次才退避 30 秒（防拉起风暴），之后计数归零。
func noteExitLifetime(lived time.Duration) time.Duration {
	notesShortLivedMu.Lock()
	defer notesShortLivedMu.Unlock()
	if lived >= 10*time.Second {
		notesShortLived = 0
		return 0
	}
	notesShortLived++
	if notesShortLived < 3 {
		return 0
	}
	notesShortLived = 0
	return 30 * time.Second
}

// notesHoldFor 开一个维护窗口：这段时间内不自动拉起库（到点自动恢复）
func notesHoldFor(d time.Duration) {
	notesHoldMu.Lock()
	notesHoldUntil = time.Now().Add(d)
	notesHoldMu.Unlock()
}

// notesHoldClear 立刻结束维护窗口（人工「启动/重启」时用）
func notesHoldClear() {
	notesHoldMu.Lock()
	notesHoldUntil = time.Time{}
	notesHoldMu.Unlock()
}

func notesHeld() bool {
	notesHoldMu.Lock()
	defer notesHoldMu.Unlock()
	return time.Now().Before(notesHoldUntil)
}

type notesSpec struct {
	Dir    string // 库仓库目录（内含 src/server.py）
	Python string // python.exe
	Port   int
	URL    string // 形如 http://127.0.0.1:8848
}

// defaultNotesDir 库在哪：环境变量优先，其次找常见位置
func defaultNotesDir() string {
	if v := os.Getenv("COREHOST_NOTES_DIR"); v != "" {
		return v
	}
	// ① **挨着平台找**（2026-10-07 辞玖：启动器拿出来就能用，不该依赖盘符目录）：
	//    包里/自己挑的目录里，库就在平台旁边；包外面再套一层（解压目录）也认。
	if exe, err := os.Executable(); err == nil {
		plat := filepath.Dir(filepath.Dir(exe)) // <平台>
		for _, cand := range []string{
			filepath.Join(plat, "..", "core-notes"),       // <解压目录>/core 与 <解压目录>/core-notes
			filepath.Join(plat, "..", "..", "core-notes"), // <解压目录>/枢阁-程序包/core 与 <解压目录>/core-notes
		} {
			if _, err := os.Stat(filepath.Join(cand, "src", "server.py")); err == nil {
				return filepath.Clean(cand)
			}
		}
	}
	// ② 本机历史默认位置
	for _, p := range []string{`D:\core-notes`, `C:\core-notes`} {
		if _, err := os.Stat(filepath.Join(p, "src", "server.py")); err == nil {
			return p
		}
	}
	return ""
}

func defaultNotesPython() string {
	if v := os.Getenv("COREHOST_NOTES_PYTHON"); v != "" {
		return v
	}
	for _, p := range []string{`C:\Python311\python.exe`, `C:\Python312\python.exe`} {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	if p, err := exec.LookPath("python"); err == nil {
		return p
	}
	return "python"
}

func defaultNotesPort() int {
	if v := os.Getenv("COREHOST_NOTES_PORT"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return 8848
}

func defaultNotesURL(port int) string {
	if v := os.Getenv("AINOTES_URL"); v != "" {
		return strings.TrimSuffix(v, "/")
	}
	return fmt.Sprintf("http://127.0.0.1:%d", port)
}

func resolveNotesSpec(dir, python string, port int, url string) notesSpec {
	if dir == "" {
		dir = defaultNotesDir()
	}
	if python == "" {
		python = defaultNotesPython()
	}
	if port == 0 {
		port = defaultNotesPort()
	}
	if url == "" {
		url = defaultNotesURL(port)
	}
	return notesSpec{Dir: dir, Python: python, Port: port, URL: strings.TrimSuffix(url, "/")}
}

func (n notesSpec) script() string { return filepath.Join(n.Dir, "src", "server.py") }
func (n notesSpec) healthURL() string {
	return n.URL + "/api/health"
}
func (n notesSpec) outLog() string {
	return filepath.Join(n.Dir, "data", "logs", "notes.out.log")
}

func notesHealthy(n notesSpec) bool {
	// 8 秒：库是单线程服务，首次混合检索要加载 bge-m3（约 60s）会把整个 HTTP 面顶住，
	// 探针给太短会把它误判成"卡死"（2026-09-14 实测踩过）
	client := &http.Client{Timeout: 8 * time.Second}
	resp, err := client.Get(n.healthURL())
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

func notesPortOpen(n notesSpec) bool {
	conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", n.Port), 800*time.Millisecond)
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}

// notesAlive 库“在不在” —— 口径就是辞玖那句“**开着就是在线**”：
//
//	· 先 TCP 连一下（便宜、无副作用）：通 → 在；
//	· 连不上再看**端口上有没有进程在监听**（netstat，结果缓存 2 秒）：
//	  有人监听 → 也算在（库是单线程服务，忙起来那一下会连不上，“忙”不等于“挂”）。
//
// 2026-09-25 实测：只看 TCP 那一下，10 分钟里误判 36 次，每次都白拉一个实例。
func notesAlive(n notesSpec) bool {
	if notesPortOpen(n) {
		return true
	}
	yes, _ := notesListenerProbe.get(func() bool { return len(notesPIDs(n)) > 0 })
	return yes
}

// notesPIDs 监听该端口的进程（netstat -ano；不认本地化状态列，只认地址与 PID）
func notesPIDs(n notesSpec) []string {
	out, err := runSilent("netstat", "-ano", "-p", "TCP").Output()
	if err != nil {
		return nil
	}
	suffix := ":" + strconv.Itoa(n.Port)
	pids := []string{}
	seen := map[string]bool{}
	for _, line := range strings.Split(string(out), "\n") {
		f := strings.Fields(strings.TrimSpace(line))
		if len(f) < 4 || !strings.HasPrefix(f[0], "TCP") {
			continue
		}
		if !strings.HasSuffix(f[1], suffix) {
			continue
		}
		pid := f[len(f)-1]
		if pid != "0" && !seen[pid] {
			seen[pid] = true
			pids = append(pids, pid)
		}
	}
	return pids
}

// spawnNotes 拉起库：无窗口、脱离控制台，输出重定向到库自己的日志。
// 返回 cmd：调用方拿它 Wait()，就能在库**退出的瞬间**知道（而不是等下一拍巡检）。
func spawnNotes(n notesSpec, launcher string) (*exec.Cmd, error) {
	if err := os.MkdirAll(filepath.Dir(n.outLog()), 0o755); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(n.outLog(), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, err
	}
	cmd := exec.Command(n.Python, "src/server.py", "--port", strconv.Itoa(n.Port))
	cmd.Dir = n.Dir
	cmd.Env = append(os.Environ(), "COREHOST_LAUNCHED_BY="+launcher)
	cmd.Stdout = f
	cmd.Stderr = f
	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: detachedProcess | createNewProcGroup,
	}
	if err := cmd.Start(); err != nil {
		_ = f.Close()
		return nil, err
	}
	_ = f.Close()
	// 注意：**不 Release** —— 我们要 Wait() 它，好在退出瞬间把库拉回来
	return cmd, nil
}

// watchNotes 盯着刚拉起的库进程：它一退出就立刻再拉（不用等巡检那一拍）。
//
// 为什么要事件驱动：辞玖要"尽可能的快"。1 秒巡检已经是兜底，进程退出事件是 0 延迟。
// 防打转：见 noteExitLifetime —— 偶发（活儿干着被杀）立刻重拉，连续 3 次短命才退避。
func watchNotes(cmd *exec.Cmd, n notesSpec, launcher string) {
	logDir := filepath.Join(n.Dir, "data", "logs")
	started := time.Now()
	pid := 0
	if cmd.Process != nil {
		pid = cmd.Process.Pid
	}
	go func() {
		_ = cmd.Wait()
		alive := time.Since(started)
		if notesAlive(n) {
			// 刚退出的不是“唯一的库”：典型就是它一看端口已经有人在答就自己退了（库侧的预探）。
			// 这不算“库挂了”——再拉就是一轮轮白拉（2026-09-25 实测 10 分钟 36 次）。
			logf(logDir, "库进程（pid %d）退出了（存活 %v），但 %s 仍有人在监听 → 不重启",
				pid, alive.Round(time.Second), n.URL)
			return
		}
		logf(logDir, "库进程（pid %d）退出了（存活 %v）→ 立刻拉起", pid, alive.Round(time.Second))
		if notesHeld() {
			logf(logDir, "（维护窗口内，先不拉；到点自动恢复）")
			return
		}
		if d := noteExitLifetime(alive); d > 0 {
			logf(logDir, "连续 3 次活不过 10 秒（疑似起不来）→ 退避 %v 再试，避免拉起风暴", d)
			time.Sleep(d)
			if notesHeld() {
				return
			}
		}
		base := launcher
		for strings.HasSuffix(base, "（退出事件）") { // 连杀会叠标签，去掉
			base = strings.TrimSuffix(base, "（退出事件）")
		}
		ensureNotesNow(n, base+"（退出事件）")
	}()
}

// ensureNotes 库的保活：**端口优先** —— 端口通就什么都不做（哪怕它不答 HTTP）：
// 库是单线程服务，"忙（首次加载模型 / 大批重建索引）"与"卡死"从外面看一模一样，
// 而我们**从不杀它**（要强启只有维护入口 POST /api/notes/restart，那是人工意图）。
// 端口不通才拉起；force=true 表示"人工要它现在就在"。
func ensureNotes(n notesSpec, launcher string, force bool) bool {
	if notesPythonOverride != "" && n.Python != notesPythonOverride {
		n.Python = notesPythonOverride // 自愈换过的解释器（见 repairNotesPython）
	}
	logDir := filepath.Join(n.Dir, "data", "logs")
	if n.Dir == "" {
		return false
	}
	if _, err := os.Stat(n.script()); err != nil {
		if transition(logDir, "notes-missing") {
			logf(logDir, "库不在位：找不到 %s（跳过库保活）", n.script())
		}
		// 说清（2026-10-10）：写一份人能读的说明 + 状态接口里带上
		writeNotesStartFailed(coreDirOf(n), n, "库的启动脚本不在位", "检查库目录是否装对；或设 COREHOST_NOTES_DIR 指过去")
		return false
	}
	// **端口优先**：端口开着 = 库在跑（单线程服务"忙"与"卡死"从外面一样，且我们从不杀它），
	// 于是稳态下这一拍只有一次 TCP 连接，不打 HTTP、不写库的访问日志。
	// 连不上时再看“端口上有没有人在监听”（notesAlive）—— 别把"忙"当成"挂"。
	if notesAlive(n) {
		if transition(logDir, "notes-healthy") {
			logf(logDir, "库在线（%s）", n.URL)
		}
		clearNotesStartFailed(coreDirOf(n)) // 库好了：那份"起不来"的说明不该再留着误导
		return false
	}

	// 自愈第一步：解释器不对就换一个能用的（2026-10-10）
	if fixed, note := repairNotesPython(n); note != "" {
		if fixed.Python != n.Python {
			logf(logDir, "库自愈：%s", note)
			n = fixed
			notesPythonOverride = fixed.Python // 下次巡检验也会用它
		} else {
			writeNotesStartFailed(coreDirOf(n), n, "没有可用的 Python 解释器", note)
			return false
		}
	}
	if notesHeld() {
		if transition(logDir, "notes-held") {
			logf(logDir, "库不在，但正在维护窗口内 → 暂不拉起（到点自动恢复）")
		}
		return false
	}
	// 拉起节流：60 秒内只拉一次。库是单线程服务，"忙"时会长时间不答，
	// 不节流就会一轮巡检拉一个，堆出一串实例（2026-09-14 实测堆到 4 个）。
	notesSpawnMu.Lock()
	if since := time.Since(lastNotesSpawn); !lastNotesSpawn.IsZero() && since < 60*time.Second {
		notesSpawnMu.Unlock()
		if transition(logDir, "notes-throttled") {
			logf(logDir, "库刚拉过 %v，本次不重复拉起（等它就绪）", since.Round(time.Second))
		}
		return false
	}
	lastNotesSpawn = time.Now()
	notesSpawnMu.Unlock()

	cmd, err := spawnNotes(n, launcher)
	if err != nil {
		logf(logDir, "拉起库失败：%v", err)
		return false
	}
	logf(logDir, "已拉起库（%s，启动者=%s）", n.Python, launcher)
	watchNotes(cmd, n, launcher)
	if waitHealthyURL(n.healthURL(), 20*time.Second) {
		transition(logDir, "notes-healthy")
		logf(logDir, "库已就绪：%s", n.URL)
		return true
	}
	logf(logDir, "拉起后 20s 内库仍未就绪，看 %s", n.outLog())
	return true
}

// ensureNotesNow 立刻确保库在跑（**绕过 60 秒拉起节流**）：退出事件与人工重启专用。
// 节流本是为了防"库在忙时被判死、一轮拉一个"，而这里是**确知端口不通**，没有误判风险。
func ensureNotesNow(n notesSpec, launcher string) bool {
	notesSpawnMu.Lock()
	lastNotesSpawn = time.Time{}
	notesSpawnMu.Unlock()
	return ensureNotes(n, launcher, true)
}

// stopNotes 停库：杀掉监听端口的进程。**只在"关闭枢阁"或维护时用**——
// 库本身没有"人工停止"语义，巡检下一轮就会把它拉回来（永远在线）。
func stopNotes(n notesSpec) int {
	logDir := filepath.Join(n.Dir, "data", "logs")
	killed := 0
	for _, pid := range notesPIDs(n) {
		if err := runSilent("taskkill", "/PID", pid, "/F").Run(); err == nil {
			killed++
		}
	}
	logf(logDir, "已停库（杀掉 %d 个进程；库没有暂停标志了，下一拍巡检会把它拉回来）", killed)
	return killed
}

// notesStatus 给面板/接口看的状态。
// healthy 走 5 秒采样缓存（页面 3 秒一轮 → 缓存吃掉重复；库是单线程，别被探针刷）；
// port_open 是**实时**的（一次 TCP 连接，便宜且没有副作用），所以"在不在"永远是新鲜的。
func notesStatus(n notesSpec) map[string]any {
	healthy, at := notesHealthProbe.get(func() bool { return notesHealthy(n) })
	st := map[string]any{
		"dir": n.Dir, "port": n.Port, "url": n.URL,
		"healthy": healthy, "port_open": notesPortOpen(n),
		"checked_at": at.Format(time.RFC3339), "ttl_s": int(notesHealthTTL.Seconds()),
	}
	if _, err := os.Stat(n.script()); err != nil {
		st["missing"] = true
	}
	return st
}

func waitHealthyURL(url string, d time.Duration) bool {
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		client := &http.Client{Timeout: 2 * time.Second}
		if resp, err := client.Get(url); err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return true
			}
		}
		time.Sleep(400 * time.Millisecond)
	}
	return false
}
