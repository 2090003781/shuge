package main

import (
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// 界面装配（2026-10-10，辞玖定的 B 方案第②块）
//
// 想解决的问题：界面（主程序页 / 库页 / 内核页）**本来就是硬盘上的文件**，
// 却要"经过"唯一地址（8790）那条网关 —— 网关没起、端口被占、被别的程序劫持，
// 界面就没了，用户看到的是浏览器那句"无法访问此页面"。
//
// 做法：主进程内再开一条**只在本机回环**上的监听，端口由系统分配（:0），
// 路由跟主监听**完全同一套**（buildMux），端口写进 data/ui-assets.port。
//
// 它**不是**第二套服务：同一个进程、同一份内存态、同一批接口。只是"从哪拿界面"这件事
// 不再和"唯一地址那条路健不健康"绑在一起。

const localAssetsPortFile = "ui-assets.port"

// 界面监听候选端口（2026-10-14 接手分析）：
//
// 原来是 net.Listen("tcp", "127.0.0.1:0")（见下面的 startLocalAssets）——**系统每次随机发一个**。
// 后果被漏了很久：壳（WebView2）的 localStorage / origin 是**按端口隔离**的，
// 端口每次不一样 = 每次都换一个「网站」→ 侧栏宽度、主题缓存这类页面自己记的东西**每次启动归零**。
// 实测对照：2026-10-14 是 22338，2026-10-13 是 62401。
//
// 改成**固定候选表**（与 servePortCandidates 同一个思路、同一段教训：
// 2026-09-27「绝不退到随机端口」）。为什么不硬写一个：
// 本机动态端口范围从 1024 起（64511 个），任何端口都可能被某个**对外连接**当临时源端口占用，
// 所以留 9 个备选；只有全被占才退回 :0（此时 localStorage 会归零，但不至于没界面）。
var localAssetsPortCandidates = func() []int {
	out := make([]int, 0, 10)
	for p := 22380; p <= 22389; p++ { // 22380 是首选
		out = append(out, p)
	}
	return out
}()

// LocalAssetsPortFilePath 界面监听端口文件（壳与自检都读它）
func LocalAssetsPortFilePath(coreDir string) string {
	return filepath.Join(coreDir, "data", localAssetsPortFile)
}

// startLocalAssets 起"只发界面"的本机监听（幂等：已经在跑就直接返回）
func (st *serveState) startLocalAssets() {
	if st.localLn != nil {
		return
	}
	// 固定候选端口按序试（2026-10-14）：随机的 :0 会让 WebView2 的 origin 每次不同，
	// 页面 localStorage 每次归零。全被占才退回 :0 —— 有界面优先于 origin 稳定。
	var ln net.Listener
	var err error
	for _, port := range localAssetsPortCandidates {
		if l, e := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", port)); e == nil {
			ln, err = l, nil
			break
		}
	}
	if ln == nil {
		ln, err = net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			logf(st.logDir, "界面监听起不来（不影响唯一地址那条）：%v", err)
			return
		}
		logf(st.logDir, "界面监听：候选端口全被占，退回随机端口（本次 origin 会变，页面本地设置可能归零）")
	}
	st.localLn = ln
	st.localPort = ln.Addr().(*net.TCPAddr).Port

	// 路由与主监听同一套
	srv := &http.Server{Handler: st.buildMux()}
	go func() {
		if err := srv.Serve(ln); err != nil && err != http.ErrServerClosed {
			logf(st.logDir, "界面监听退出：%v", err)
		}
	}()

	f := LocalAssetsPortFilePath(st.coreDir)
	_ = os.MkdirAll(filepath.Dir(f), 0o755)
	_ = os.WriteFile(f, []byte(fmt.Sprintf("%d\n", st.localPort)), 0o644)
	logf(st.logDir, "界面装配：本机监听已就绪 http://127.0.0.1:%d/console（窗口版与网页版都可用它开界面）", st.localPort)
}

// ReadLocalAssetsPort 读界面监听端口；文件不在/不合法 → 0
func ReadLocalAssetsPort(coreDir string) int {
	b, err := os.ReadFile(LocalAssetsPortFilePath(coreDir))
	if err != nil {
		return 0
	}
	var v int
	if _, err := fmt.Sscanf(strings.TrimSpace(string(b)), "%d", &v); err == nil && v > 0 && v < 65536 {
		return v
	}
	return 0
}

// localAssetsAlive 界面监听还活着吗（端口通 + /healthz 应答）——
// 进程退了这个文件会留下，所以**文件在不算数**，必须探一下。
func localAssetsAlive(coreDir string) int {
	port := ReadLocalAssetsPort(coreDir)
	if port == 0 {
		return 0
	}
	c, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), 500*time.Millisecond)
	if err != nil {
		return 0
	}
	_ = c.Close()
	cl := &http.Client{Timeout: 3 * time.Second}
	resp, err := cl.Get(fmt.Sprintf("http://127.0.0.1:%d/healthz", port))
	if err != nil {
		return 0
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return 0
	}
	return port
}
