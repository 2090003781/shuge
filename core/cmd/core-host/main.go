//go:build windows

// 枢阁 · 主进程（笔记软件本体；无窗口、常驻；内核 core 是它的附属进程）
//
// 为什么是 Go + GUI 子系统（2026-09-11 辞玖反馈「时不时弹出命令行窗口」）：
//
//	· 计划任务每 2 分钟拉起一次 powershell.exe → 即使 -WindowStyle Hidden，控制台窗口仍会闪一下
//	· 用 `go build -ldflags "-H windowsgui"` 编译 = 本程序**不分配控制台**，从根上没有窗口
//
// 职责（越简单越可靠）：探测 /healthz → 不在则拉起 core → 只记状态跃迁。
// 为什么必须有它：账本/总线/插件都在 core 进程内，core 一死，平台内**没有任何东西**能救它。
//
// 三种运行方式（2026-09-12 起）：
//
//	core-host.exe                计划任务每 2 分钟调用 → 确保「启停台」常驻（巡检由启停台负责）
//	core-host.exe --serve        常驻控制台：控制页面在 127.0.0.1:8790（唯一对外地址），
//	                               负责启停 + 每 1s 保活巡检（常驻自检每 5 分钟一轮）
//	core-host.exe --force-start  一次性拉起（桌面「★启动枢阁」）
//	core-host.exe --restart      等旧实例退干净再拉起（core 里的旧版兼容路径）
//
// 三条铁律（都是踩出来的）：
//  1. 单实例锁（internal/singlelock）参与所有判断：只看 /healthz 不够——core 停机时 HTTP 先关、
//     进程还要几秒收尾，只等 /healthz 会让两个 core 同时写账本（2026-09-12 实测 seq 交叉损坏）。
//  2. 熔断：10 分钟内自动拉起 6 次仍不好 → 停手 + 写 core.quarantine，等人工（启停台/桌面图标）。
//  3. 归属：每次拉起都带上"启动者"（环境变量 COREHOST_LAUNCHED_BY）→ core 写 owner.json + 进账本。
package main

import (
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"core/internal/singlelock"
)

var (
	defaultCoreDir = coreDirFromExe()
)

const (
	detachedProcess    = 0x00000008 // DETACHED_PROCESS：不建控制台
	createNewProcGroup = 0x00000200 // CREATE_NEW_PROCESS_GROUP
	// 内核（core）的内部端口：只有主进程跟它说话；对外只有一个地址（见 servePortCandidates）
	defaultHealthURL = "http://127.0.0.1:8795/healthz"
	defaultAddr      = "127.0.0.1:8795"
	lockName         = "core"
	// 启停台（对外控制台那个进程）自己的单实例锁。
	// 为什么要它（2026-09-27 实测）：只靠 /healthz 不够 —— 机器内存紧时 2 秒探不到，
	// 新实例就当“没在跑”去占候选端口（6 个用完就跳随机端口），
	// 结果堆了 20 多个启停台、还把端口文件改成随机端口 → 更探不到 → 越滚越多。
	hostLockName = "core-host"

	startWindow    = 10 * time.Minute // 熔断窗口
	maxStartsInWin = 6                // 窗口内最多自动拉起次数
)

// coreDirFromExe 平台根 = 可执行件所在目录的上一层（bin\core-host.exe → 平台根）。
// 推不出来才退回 D:\core（本机历史值）。包解压到任意位置、直接双击 core\bin\core-host.exe 都能用。
func coreDirFromExe() string {
	if exe, err := os.Executable(); err == nil {
		d := filepath.Dir(filepath.Dir(exe))
		if st, err := os.Stat(filepath.Join(d, "bin", "core.exe")); err == nil && !st.IsDir() {
			return d
		}
		if st, err := os.Stat(filepath.Join(d, "cmd")); err == nil && st.IsDir() {
			return d
		}
	}
	return `D:\core`
}

func main() {
	coreDir := flag.String("dir", defaultCoreDir, "core 所在目录")
	url := flag.String("url", defaultHealthURL, "健康探针地址")
	waitSec := flag.Int("wait", 12, "拉起后等待就绪的秒数")
	forceStart := flag.Bool("force-start", false, "无视暂停标志强制拉起（桌面图标/启停台用）")
	restartMode := flag.Bool("restart", false, "等 core 退出后拉起新实例（控制台「重启」用）")
	serveMode := flag.Bool("serve", false, "常驻启停台：127.0.0.1:8791，负责启停与巡检")
	patrolMin := flag.Int("patrol", int(patrolDefaultEvery.Minutes()), "常驻巡检间隔（分钟；0=关闭）")
	openWeb := flag.Bool("open-web", false, "拉起 core（如未在跑）并在默认浏览器打开 WebUI（桌面图标用）")
	by := flag.String("by", "", "启动者标签（写进 core 的 owner 登记；空 = 按启动方式自动命名）")
	notesDir := flag.String("notes-dir", "", "库（ai-notes）目录；空 = 自动探测（默认 D:\\core-notes）")
	notesPython := flag.String("notes-python", "", "库用的 python.exe；空 = 自动探测")
	notesPort := flag.Int("notes-port", 0, "库监听端口（默认 8848）")
	notesURL := flag.String("notes-url", "", "库的基地址（默认 http://127.0.0.1:<port>）")
	flag.Parse()

	// 库是**主体**：宿主先保它，再保内核（内核停着库照用；库停了笔记就没了）
	ns := resolveNotesSpec(*notesDir, *notesPython, *notesPort, *notesURL)

	launcher := *by
	if launcher == "" {
		switch {
		case *serveMode:
			launcher = "枢阁主进程（常驻）"
		case *openWeb:
			launcher = "桌面图标（枢阁）"
		case *restartMode:
			launcher = "枢阁控制台（重启内核）"
		case *forceStart:
			launcher = "枢阁主进程（手动拉起）"
		default:
			launcher = "计划任务（保活）"
		}
	}

	logDir := filepath.Join(*coreDir, "data", "logs")
	// 暂停标志路径由 checkAndStart 内部自己算（一次性与常驻两条路径共用同一判据）
	quarantinePath := filepath.Join(*coreDir, "data", "core.quarantine")
	startsPath := filepath.Join(logDir, "host.starts")
	_ = os.MkdirAll(logDir, 0o755)
	touch(filepath.Join(logDir, "host.last")) // 每次运行留时间戳，确认计划任务确实触发

	// 常驻模式：起 HTTP 启停台后就不再返回（巡检在内部循环）
	if *serveMode {
		runServe(*coreDir, *url, launcher, time.Duration(*patrolMin)*time.Minute, ns)
		return
	}

	// 桌面图标语义：把 WebUI 打开给我看 —— 该起 core 就起，起不来就退到启停台页面（那里有日志）
	if *openWeb {
		_ = os.Remove(quarantinePath)
		_ = os.Remove(startsPath)
		setHostPaused(*coreDir, false) // 桌面图标 = 我要打开：清掉「人工关闭」标志
		// ⚠️ 2026-10-07 辞玖：「我点这个启动咋没反应？」
		//    以前这里只看内核（healthy(8795)）：**内核在跑、启停台却不在**时，
		//    启动器不启启停台 → 8790 没人听 → 浏览器打开的是"连不上"，看着就是"没反应"。
		//    改成：只要 8790 没人听，就把启停台拉起来（这才叫"启动器"）。
		if !portListening(ReadPort(*coreDir)) {
			spawnServe(*coreDir, *url, *patrolMin, ns)
			_ = waitPort(ReadPort(*coreDir), 8*time.Second)
		} else if hostPaused(*coreDir) {
			// 启停台在、内核停着：这就是"开着但内核没开"的正常态，直接开页面给它看
			logf(logDir, "启停台已在跑（8790 有人听），直接打开页面（内核状态见页面）")
		}
		ensureNotes(ns, launcher, true) // 主体优先：先把库拉起来
		if !healthy(*url) {
			checkAndStart(*coreDir, *url, launcher, true, ns)
			_ = waitHealthy(*url, time.Duration(*waitSec)*time.Second)
		}
		// 打开的是**主程序页**（带「控制台 / 库 / 内核」三个页签）——辞玖 2026-10-06：
		// 「打开顺序固定好，先主程序然后是库最后启动内核」。所以图标先把**主程序**给你看，
		// 从页签进库/内核（库本身也挂在 /notes/ 上，内核在 8795）。
		target := fmt.Sprintf("http://127.0.0.1:%d/console", ReadPort(*coreDir))
		if healthy(*url) {
			logf(logDir, "已在默认浏览器打开枢阁：%s（内核运行时透明代理；启动者=%s）", target, launcher)
		} else {
			logf(logDir, "已在默认浏览器打开枢阁：%s（内核未就绪 → 外壳页，可直接点启动；启动者=%s）", target, launcher)
		}
		openBrowser(target, logDir)
		return
	}

	// 重启模式：等旧实例**真的**退出（最多 ~30s）。
	// 判据是单实例锁而不是 /healthz：core 停机时 HTTP 先关、进程还要几秒收尾。
	if *restartMode {
		waited := false
		for i := 0; i < 60; i++ {
			if !lockBusy() && !healthy(*url) {
				break
			}
			if !waited {
				logf(logDir, "重启：等待旧实例彻底退出（单实例锁 + /healthz 双判据）")
				waited = true
			}
			time.Sleep(500 * time.Millisecond)
		}
		if lockBusy() {
			logf(logDir, "重启失败：旧实例 30s 内仍未释放单实例锁，放弃拉起（避免双写账本）")
			os.Exit(4)
		}
	}

	// 人工入口（桌面图标 --force-start / 控制台重启 --restart）代表明确意图 → 清掉暂停与熔断
	if *forceStart || *restartMode {
		_ = os.Remove(quarantinePath)
		_ = os.Remove(startsPath)
	}
	if *forceStart {
		setHostPaused(*coreDir, false) // 手动拉起 = 打开枢阁
	}

	// 计划任务（无参）的主职责已经变成"确保启停台常驻"——巡检由它每 60s 负责，本进程不重复巡检。
	// 注意：这一步必须在 healthy 检查**之前**——否则 core 正常时计划任务直接返回，
	// 启停台就永远起不来（2026-09-12 实测：core 健康 → 无人保活，8791 连不上）。
	if !*forceStart && !*restartMode {
		// 人工在控制台按过「关闭枢阁」→ 计划任务（无参、每 2 分钟）不再把软件拉起来。
		// 重新打开：桌面图标「★启动枢阁」（--open-web）或 --force-start 会清掉这个标志。
		if hostPaused(*coreDir) {
			if transition(logDir, "host-paused") {
				logf(logDir, "已置「人工关闭」标志（data/host.paused）→ 本次不拉起（桌面图标可重新打开）")
			}
			return
		}
		// 库是主体：即使启停台已经在跑，也顺手确认库还在（用户关掉浏览器不等于库该停）
		if serveAlive(*coreDir) {
			ensureNotes(ns, launcher, false)
			return
		}
		// 先记状态再去拉：否则会和启停台自己写的 healthy 抢（末尾状态会停在 serve-up，面板显示不准）
		if transition(logDir, "serve-up") {
			logf(logDir, "启停台未在跑 → 正在拉起（常驻、无窗口）；巡检交给它")
		}
		if spawnServe(*coreDir, *url, *patrolMin, ns) {
			return
		}
		logf(logDir, "启停台拉起失败 → 本次仍按一次性巡检兜底")
	} else if !serveAlive(*coreDir) {
		// 人工入口顺带保活：否则下次计划任务（最多 2 分钟）之前，页面上没有启停台可用
		spawnServe(*coreDir, *url, *patrolMin, ns)
	}

	// 人工入口（桌面图标 / --force-start）也先把库拉起来：主体优先
	ensureNotes(ns, launcher, *forceStart || *restartMode)

	if healthy(*url) {
		if transition(logDir, "healthy") {
			logf(logDir, "core 健康（首次确认或从异常恢复）")
		}
		return
	}

	checkAndStart(*coreDir, *url, launcher, *forceStart || *restartMode, ns)

	// 一次性拉起后等就绪（只为给人看结果；巡检由启停台持续负责）
	deadline := time.Now().Add(time.Duration(*waitSec) * time.Second)
	for time.Now().Before(deadline) {
		time.Sleep(500 * time.Millisecond)
		if healthy(*url) {
			if transition(logDir, "healthy") {
				logf(logDir, "core 已拉起并就绪（/healthz 200）")
			}
			return
		}
	}
	if *forceStart || *restartMode {
		logf(logDir, "拉起后 %ds 内 /healthz 仍不通，请查 data\\logs\\core.log", *waitSec)
	}
}

// checkAndStart 共享巡检逻辑（计划任务 / 启停台 / 一次性拉起都用它）。
// force=true 表示"人工意图"：跳过暂停标志与熔断检查（人工永远优先于自动保护）。
func checkAndStart(coreDir, url, launcher string, force bool, ns notesSpec) {
	// **启动顺序固定**（辞玖 2026-10-06）：① 主程序/启停台（本进程）→ ② 库 → ③ 内核。
	// 库是主体（内核停着库照用；库停了笔记就没了），所以"要起内核"这件事必须**先把库保起来**。
	// 放在这里 = 所有拉内核的路径（巡检/页面按钮/图标/看门狗代发）都自动按这个顺序，不会各写一套。
	if ns.Dir != "" {
		ensureNotes(ns, launcher, false)
	}
	logDir := filepath.Join(coreDir, "data", "logs")
	pausedPath := filepath.Join(coreDir, "data", "core.paused")
	quarantinePath := filepath.Join(coreDir, "data", "core.quarantine")
	startsPath := filepath.Join(logDir, "host.starts")

	if healthy(url) {
		if transition(logDir, "healthy") {
			logf(logDir, "core 健康（首次确认或从异常恢复）")
		}
		return
	}

	if !force {
		// 暂停标志：启停台/控制台按了「停止」→ 不自动拉起（要起就用启停台的「启动」）
		if _, err := os.Stat(pausedPath); err == nil {
			if transition(logDir, "paused") {
				logf(logDir, "已置暂停标志（人工停止）→ 不自动拉起；启停台「启动」可再启")
			}
			return
		}
		// 熔断（崩溃循环隔离）：窗口内反复拉起 = core 起不来，再拉也是白拉
		if _, err := os.Stat(quarantinePath); err == nil {
			if transition(logDir, "quarantined") {
				logf(logDir, "已被熔断隔离：%s 存在 → 不自动拉起。看 core.log 定位后用启停台「启动」恢复", quarantinePath)
			}
			return
		}
		if n := recentStarts(startsPath, startWindow); n >= maxStartsInWin {
			_ = os.WriteFile(quarantinePath,
				[]byte(fmt.Sprintf("%s 窗口 %v 内已拉起 %d 次\n", time.Now().Format("2006-01-02 15:04:05"), startWindow, n)), 0o644)
			if transition(logDir, "quarantined") {
				logf(logDir, "熔断：%v 内已拉起 %d 次（阈值 %d）→ 停止自动拉起，等人工检查 core.log", startWindow, n, maxStartsInWin)
			}
			return
		}
	}

	// 单实例锁还在 = 有 core 进程活着（可能正在启动、也可能卡死）：先不动手。
	// 这条挡掉"启动窗口期"的重复拉起（core 启动要 1~2 秒才通 /healthz）。
	if lockBusy() {
		// 分清两种"锁被持有但 /healthz 不通"：
		//   · 能答话（如 503 降级）→ 内核活着，重启也治不了病根 → 不重启，等人工/巡检报出来
		//   · 不能答话 → 正在启动的窗口期，或真卡死 → 也不动手（避免和启动中的实例抢）
		if kernelAnsweringURL(url) {
			if transition(logDir, "degraded") {
				logf(logDir, "内核在跑但自报不健康（/healthz 非 200）→ 不重启：重启治不了病根，等人工处理")
			}
			return
		}
		if transition(logDir, "starting") {
			logf(logDir, "已有实例持有单实例锁但 /healthz 不通（正在启动或卡住）→ 本次不重复拉起")
		}
		return
	}

	// 不健康：端口通但探针不过 = core 卡住 → 强杀后拉起；端口不通 = 没在跑 → 直接拉起
	if portOpen() {
		if pids := tasklistPIDs("core.exe"); len(pids) > 0 {
			logf(logDir, "core 端口在但 /healthz 不通（pid=%s）→ 判定卡死，强杀后拉起", strings.Join(pids, ","))
			for _, pid := range pids {
				_ = runSilent("taskkill", "/PID", pid, "/F").Run()
			}
			time.Sleep(2 * time.Second)
		}
	} else if transition(logDir, "down") {
		logf(logDir, "core 不在 → 拉起")
	}

	coreExe := filepath.Join(coreDir, "bin", "core.exe")
	if _, err := os.Stat(coreExe); err != nil {
		logf(logDir, "拉起失败：找不到 %s", coreExe)
		return
	}
	recordStart(startsPath)
	if err := spawn(coreExe, coreDir, launcher); err != nil {
		logf(logDir, "拉起失败：%v", err)
		return
	}
	logf(logDir, "已拉起 core（启动者=%s）", launcher)
	// 拉起后确认就绪再置状态：否则面板上"巡检状态"会停留在上一次跃迁的 down
	if waitHealthy(url, 15*time.Second) {
		transition(logDir, "healthy")
	}
}

// openBrowser 用系统默认程序打开 URL（GUI 子系统进程，不弹控制台）
// portListening 这个本机端口有没有人在听（"启停台在不在"的判据 —— 不看内核）。
func portListening(port int) bool {
	if port <= 0 {
		return false
	}
	c, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), 600*time.Millisecond)
	if err != nil {
		return false
	}
	_ = c.Close()
	return true
}

// waitPort 等端口开始有人听（最多等 d），返回是否等到。
func waitPort(port int, d time.Duration) bool {
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if portListening(port) {
			return true
		}
		time.Sleep(250 * time.Millisecond)
	}
	return portListening(port)
}

func openBrowser(url, logDir string) {
	cmd := exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	if err := cmd.Start(); err != nil {
		logf(logDir, "打开浏览器失败：%v（请手动访问 %s）", err, url)
		return
	}
	_ = cmd.Process.Release()
}

// waitHealthy 等 /healthz 通（最多 d）
func waitHealthy(url string, d time.Duration) bool {
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if healthy(url) {
			return true
		}
		time.Sleep(500 * time.Millisecond)
	}
	return false
}

// ---- 启停台保活 ----

// serveAlive 启停台是否在跑（端口从 data/serve.port 读，可能不是默认端口）
func serveAlive(coreDir string) bool {
	// 先看单实例锁：锁在 = 启停台在（哪怕它正忙、HTTP 一时答不上）
	if lockBusyName(hostLockName) {
		return true
	}
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get(fmt.Sprintf("http://127.0.0.1:%d/healthz", ReadPort(coreDir)))
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

func spawnServe(coreDir, url string, patrolMin int, ns notesSpec) bool {
	exe, err := os.Executable()
	if err != nil {
		return false
	}
	args := []string{"--serve", "--dir", coreDir, "--url", url, "--patrol", fmt.Sprint(patrolMin)}
	if ns.Dir != "" {
		args = append(args, "--notes-dir", ns.Dir, "--notes-python", ns.Python,
			"--notes-port", fmt.Sprint(ns.Port), "--notes-url", ns.URL)
	}
	cmd := exec.Command(exe, args...)
	cmd.Dir = coreDir
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: detachedProcess | createNewProcGroup}
	if err := cmd.Start(); err != nil {
		return false
	}
	_ = cmd.Process.Release()
	// 等它就绪；就绪失败也算"已交给它"，下一次计划任务（2 分钟后）会再兜底
	for i := 0; i < 10; i++ {
		time.Sleep(300 * time.Millisecond)
		if serveAlive(coreDir) {
			return true
		}
	}
	return true
}

// ---- 进程与锁 ----

func lockBusy() bool { return singlelock.Busy(lockName) }

// lockBusyName 探某把命名锁是否被别的进程持有
func lockBusyName(name string) bool { return singlelock.Busy(name) }

func killCore() {
	for _, pid := range tasklistPIDs("core.exe") {
		_ = runSilent("taskkill", "/PID", pid, "/F").Run()
	}
}

func healthy(url string) bool {
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

// portOpen 端口是否有人监听（区分"没在跑"与"卡住"）
func portOpen() bool {
	conn, err := net.DialTimeout("tcp", defaultAddr, 800*time.Millisecond)
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}

// runSilent 起一个控制台小程序：**不建控制台、不弹窗、也不留隐藏 conhost**。
// DETACHED_PROCESS = 子进程既不继承也不新建控制台（见 supervisor/cmd_windows.go 的实测结论：
// CREATE_NO_WINDOW 仍会各带一个隐藏 conhost）；HideWindow 兜底 STARTF_USESHOWWINDOW。
// 输出仍可正常用管道收（.Output()），脱离控制台不影响重定向。
func runSilent(name string, args ...string) *exec.Cmd {
	cmd := exec.Command(name, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: detachedProcess | createNewProcGroup,
	}
	return cmd
}

func tasklistPIDs(image string) []string {
	out, err := runSilent("tasklist", "/FI", "IMAGENAME eq "+image, "/FO", "CSV", "/NH").Output()
	if err != nil {
		return nil
	}
	pids := []string{}
	for _, line := range strings.Split(string(out), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || !strings.HasPrefix(line, `"`) {
			continue
		}
		cols := strings.Split(line, `","`)
		if len(cols) >= 2 {
			pids = append(pids, strings.Trim(cols[1], `"`))
		}
	}
	return pids
}

func spawn(exe, dir, launchedBy string) error {
	cmd := exec.Command(exe)
	cmd.Dir = dir
	// 把"谁拉起的"传给 core（core 写进 data/owner.json 与账本 platform.core.started）
	cmd.Env = append(os.Environ(), "COREHOST_LAUNCHED_BY="+launchedBy)
	// DETACHED_PROCESS：core **既不继承也不新建控制台**（它自己写 data/logs/core.log）
	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: detachedProcess | createNewProcGroup,
	}
	if err := cmd.Start(); err != nil {
		return err
	}
	// 不等待：core 要长期存活（本进程退出后它继续跑）
	return cmd.Process.Release()
}

// ---- 熔断计数（崩溃循环的判据）----

// recentStarts 窗口内已拉起次数（顺带把过期记录清掉，避免文件无限增长）
func recentStarts(path string, window time.Duration) int {
	raw, _ := os.ReadFile(path)
	cut := time.Now().Add(-window)
	keep := []string{}
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		t, err := time.Parse(time.RFC3339, line)
		if err != nil {
			continue
		}
		if t.After(cut) {
			keep = append(keep, line)
		}
	}
	// 注意：必须补回行尾换行，否则 recordStart 的追加会与最后一行粘在一起（曾导致计数永远涨不上去）
	out := ""
	if len(keep) > 0 {
		out = strings.Join(keep, "\n") + "\n"
	}
	_ = os.WriteFile(path, []byte(out), 0o644)
	return len(keep)
}

func recordStart(path string) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	_, _ = f.WriteString(time.Now().Format(time.RFC3339) + "\n")
}

// ---- 日志与状态（只在跃迁时写，避免每分钟一条的噪音）----

func logf(logDir, format string, args ...any) {
	f, err := os.OpenFile(filepath.Join(logDir, "host.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	line := fmt.Sprintf("%s | %s\n", time.Now().Format("2006-01-02 15:04:05"), fmt.Sprintf(format, args...))
	_, _ = f.WriteString(line)
}

func transition(logDir, state string) bool {
	path := filepath.Join(logDir, "host.state")
	prev, _ := os.ReadFile(path)
	if strings.TrimSpace(string(prev)) == state {
		return false
	}
	_ = os.WriteFile(path, []byte(state), 0o644)
	return true
}

func touch(path string) {
	_ = os.WriteFile(path, []byte(time.Now().Format("2006-01-02 15:04:05")), 0o644)
}

// kernelAnsweringURL 该 URL 的服务能不能答话（任何状态码都算）
func kernelAnsweringURL(url string) bool {
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	return true
}
