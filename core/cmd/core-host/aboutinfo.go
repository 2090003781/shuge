//go:build windows

// 本文件：**「这台机器上 / 关于枢阁」要的那些事实，一处算清**（2026-10-11）。
//
// 辞玖问：「这部分在新机上不同位置能自动更新吗」。
// 答：能，但**不能靠猜** —— 所以这一页的每一行都从"当下真实的位置"推：
//
//	| 行       | 从哪来                                        | 换台机器/换目录会自动变吗 |
//	|----------|-----------------------------------------------|--------------------------|
//	| 主进程   | 进程自报 hostVersion + pid                     | 是（常量 + 进程）        |
//	| 应用根   | **exe 自己所在位置**（bin 的上一层 / core 的再一层） | **是**（解压到哪都对）   |
//	| 平台目录 | 同上（core）                                   | 是                       |
//	| 库目录   | config.yaml 里那份 vault.path > 正在跑的库自报   | 是                       |
//	| 界面地址 | 本机那条"只发界面"的监听端口（壳用的就是它）     | 是                       |
//	| 窗口     | 壳自己写的 data/shell-state.json                | 是（壳每做一次动作就重写）|
//	| 窗口状态 | 壳的 %LOCALAPPDATA%\Shuge\desktop-shell.json    | 是（按用户目录推）       |
package main

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

// appRootOf 从**主进程 exe 自己所在位置**推"应用根"（也就是包里那一层）。
//
// 包里的形状是：<应用根>\core\bin\core-host.exe
// 所以先试上两层（core），再看那下面有没有 core-notes（库目录）；
// 没有就退回上一层（有些人喜欢把 core 里面的东西摊平）。
func appRootOf(coreDir string) string {
	bin := filepath.Dir(os.Args[0]) // <应用根>\core\bin
	cand := filepath.Dir(bin)       // <应用根>\core
	root := filepath.Dir(cand)      // <应用根>
	if strings.EqualFold(filepath.Base(cand), "core") && dirExists(root) {
		return root
	}
	return filepath.Dir(coreDir)
}

func dirExists(p string) bool {
	st, err := os.Stat(p)
	return err == nil && st.IsDir()
}

// vaultPathFromConfig 从 config.yaml 里抠 resources.vault.path（抠不到返回 ""）。
//
// 相对路径按**应用根**解析（比如包里写 "core-notes" → <应用根>\core-notes）——
// 这样一份配置跟着包走，换台机器、换个目录解压都对（2026-10-11 虚拟机实测：
// 老包里写的是打包机的绝对路径 D:\个人数据\辞玖，到了客人机上那一行就是错的）。
func vaultPathFromConfig(coreDir string) string {
	b, err := os.ReadFile(filepath.Join(coreDir, "config.yaml"))
	if err != nil {
		return ""
	}
	lines := strings.Split(string(b), "\n")
	for i, ln := range lines {
		if !strings.HasPrefix(strings.TrimSpace(ln), "vault:") {
			continue
		}
		for j := i; j < i+4 && j < len(lines); j++ {
			t := strings.TrimSpace(lines[j])
			if strings.HasPrefix(t, "path:") {
				v := strings.TrimSpace(strings.TrimPrefix(t, "path:"))
				// 去掉行尾注释：`path: "core-notes"   # 跟着包走` —— 打包器会在后面写一句说明
				if i := strings.Index(v, "#"); i >= 0 {
					v = strings.TrimSpace(v[:i])
				}
				v = strings.Trim(v, `"'`)
				v = filepath.FromSlash(v)
				if v != "" && !filepath.IsAbs(v) {
					v = filepath.Join(appRootOf(coreDir), v)
				}
				return v
			}
		}
	}
	return ""
}

// handleAbout GET /api/host/about —— 「这台机器上 / 关于枢阁」要的那几行事实。
//
// ⚠️ 口径（2026-10-11 虚拟机实测后改的）：
//
//	**"库目录"这一行 = 实际会用的那个**（主进程启动时解析出来的 notesSpec.Dir），
//	不是"config.yaml 里写的那个"。老包里 config 写的是**打包机**的绝对路径，
//	在客人机上根本不存在 —— 拿它当"库目录"显示就是误导（客人机上实测显示成 D:\个人数据\辞玖）。
//	config 里写的单独用 vault_config 报出来，界面在两者不一致时并列显示，好排查。
func (st *serveState) handleAbout(w http.ResponseWriter, r *http.Request) {
	inUse := notesDirOf(st.notes) // 实际会用的
	fromCfg := vaultPathFromConfig(st.coreDir)
	vaultFrom := "自动探测（包内 core-notes）"
	if fromCfg != "" && samePath(fromCfg, inUse) {
		vaultFrom = "config.yaml"
	} else if fromCfg != "" {
		vaultFrom = "自动探测（config.yaml 里写的是另一处，见下）"
	}
	uiPort := st.localPort
	if uiPort == 0 {
		uiPort = st.port
	}
	writeJSON(w, map[string]any{
		"ok":              true,
		"version":         hostVersion,
		"pid":             os.Getpid(),
		"app_root":        appRootOf(st.coreDir),
		"core_dir":        st.coreDir,
		"vault":           inUse,
		"vault_from":      vaultFrom,
		"vault_config":    fromCfg,                             // config.yaml 里写的（可能不存在）
		"vault_config_ok": fromCfg != "" && dirExists(fromCfg), // 它在这台机器上存在吗
		"vault_resolved":  inUse,
		"console":         "http://127.0.0.1:" + itoa(st.port) + "/console",
		"ui_assets":       uiPort,
		"user_dir":        os.Getenv("USERPROFILE"),
		"config_file":     filepath.Join(st.coreDir, "config.yaml"),
	})
}

// samePath 两个路径是不是同一个（忽略大小写与末尾斜杠；Windows 上够用）。
func samePath(a, b string) bool {
	n := func(p string) string {
		return strings.ToLower(strings.TrimRight(filepath.Clean(p), `\/`))
	}
	return n(a) == n(b)
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		b[i] = '-'
	}
	return string(b[i:])
}

// notesDirOf "这套东西自己认的库目录"：主进程启动时算好的那个（notesSpec.Dir）。
//
// 说明：这里刻意**不**去问"正在跑的库"，因为那一份可能被环境变量指到别处（开发机上很常见），
// 而这一页要说的是"这台机器上这套东西的位置" —— 以本套自己的解析结果为准。
func notesDirOf(ns notesSpec) string { return ns.Dir }
