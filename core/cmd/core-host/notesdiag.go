package main

import (
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
)

// 库启动自愈与"起不来说清"（2026-10-10，B 方案第④块的后半）
//
// 辞玖：「起不来服务应该是代码的问题，不要这个页面，确保永远能够启动。」
//
// 现状（读完代码的结论）：`ensureNotes` 只在**拉起失败**时写一行日志；下面是三种真实情况：
//   ① 脚本不在位（库没装/路径不对）      → 只记一次 transition，页面上只看到"库不在"
//   ② Python 解释器不对（没装/版本不对）  → 拉起失败一行日志
//   ③ Python 在，但缺依赖（yaml/torch…）  → 进程起来 1 秒后退出，日志里只有子进程的 traceback，
//                                          而"为什么"（哪个包装不上、哪条命令能修）没人说
// 于是用户看到的是"库一会儿有一会儿没有"，而且**没有任何可查的东西**。
//
// 这里做两件事：
//   ① **先说清**：写 `data/notes-start-failed.txt` —— 哪个 python、什么命令、还缺哪些包（带 pip 一行）、
//      以及子进程最后几行报错；同时把这些塞进 /api/host/status 的 notes 段（页面能看到）。
//   ② **能自愈**：解释器不在就直接换一个能用的（python3 / py -3 / PATH 里的 python）。
//
// 向量检索/模型那一层**不碰**（辞玖 2026-10-10 明确留后续）：缺 torch / sentence-transformers
// 这类只提示、不擅自装。

// notesImportToPip import 名 → pip 包名（不一样的那些要写清，否则提示里给出的命令装不上）
var notesImportToPip = map[string]string{
	"yaml":                  "pyyaml",
	"PIL":                   "Pillow",
	"fitz":                  "PyMuPDF",
	"docx":                  "python-docx",
	"frontmatter":           "python-frontmatter",
	"cryptography":          "cryptography",
	"numpy":                 "numpy",
	"sentence_transformers": "sentence-transformers",
	"torch":                 "torch",
}

// notesModulesNeeded import 名（缺了会起不来或降级）。向量/模型那几个**只提示不擅自装**（辞玖 2026-10-10）
var notesModulesNeeded = []string{
	"yaml", "numpy", "PIL", "fitz", "docx", "frontmatter", "cryptography",
	"sentence_transformers", "torch",
}

// pipNamesOf 把 import 名翻成 pip 包名（给"装什么"的命令用）
func pipNamesOf(mods []string) []string {
	out := make([]string, 0, len(mods))
	for _, m := range mods {
		if p, ok := notesImportToPip[m]; ok {
			out = append(out, p)
		} else {
			out = append(out, m)
		}
	}
	return out
}

// 依赖探针要起一个 Python 进程 import 一堆包（torch 尤其慢）——**不能**在状态接口里同步跑，
// 否则 /api/host/status 会被拖到几十秒（2026-10-10 当场撞上：探针一加，状态接口就超时）。
// 做法：5 分钟缓存 + 后台预热；缓存没过期就直接用上次结果，绝不阻塞。
var (
	notesModMu       sync.Mutex
	notesModCache    []string
	notesModCachedAt time.Time
	notesModBusy     bool
)

const notesModTTL = 5 * time.Minute

// notesProbeModulesCached 取依赖缺失清单（命中缓存就立刻返回；没命中就**后台**探，先回上次结果）
func notesProbeModulesCached(python string) []string {
	notesModMu.Lock()
	fresh := time.Since(notesModCachedAt) < notesModTTL && notesModCache != nil
	busy := notesModBusy
	cached := notesModCache
	notesModMu.Unlock()
	if fresh {
		return cached
	}
	if !busy {
		notesModMu.Lock()
		notesModBusy = true
		notesModMu.Unlock()
		go func() {
			miss := notesProbeModules(python)
			notesModMu.Lock()
			notesModCache = miss
			notesModCachedAt = time.Now()
			notesModBusy = false
			notesModMu.Unlock()
		}()
	}
	return cached
}

// notesProbeModules 在指定解释器上探依赖，返回缺失清单（空 = 都在）
func notesProbeModules(python string) []string {
	if python == "" {
		return nil
	}
	code := "import importlib,sys\n" +
		"miss=[]\n" +
		"for m in %s:\n" +
		"    try: importlib.import_module(m)\n" +
		"    except Exception: miss.append(m)\n" +
		"print(','.join(miss))\n"
	list := "[" + strings.Join(quoteAll(notesModulesNeeded), ",") + "]"
	cmd := exec.Command(python, "-c", fmt.Sprintf(code, list))
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	out, err := cmd.Output()
	if err != nil {
		return []string{"（解释器本身跑不起来：" + err.Error() + "）"}
	}
	s := strings.TrimSpace(string(out))
	if s == "" {
		return nil
	}
	return strings.Split(s, ",")
}

func quoteAll(xs []string) []string {
	out := make([]string, 0, len(xs))
	for _, x := range xs {
		out = append(out, "'"+x+"'")
	}
	return out
}

// pythonUsable 这个解释器能不能跑（版本 + 能不能 import 基本件）
func pythonUsable(python string) bool {
	if python == "" {
		return false
	}
	if _, err := os.Stat(python); err != nil {
		// 允许是命令名（PATH 里的）
		if _, err2 := exec.LookPath(python); err2 != nil {
			return false
		}
	}
	cmd := exec.Command(python, "-c", "import sys;print(sys.version_info[0])")
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	out, err := cmd.Output()
	return err == nil && strings.TrimSpace(string(out)) == "3"
}

// repairNotesPython 解释器不可用时换一个能用的（自愈第一步）
func repairNotesPython(n notesSpec) (notesSpec, string) {
	if pythonUsable(n.Python) {
		return n, ""
	}
	cands := []string{}
	if v := os.Getenv("COREHOST_NOTES_PYTHON"); v != "" {
		cands = append(cands, v)
	}
	cands = append(cands,
		`C:\Python311\python.exe`, `C:\Python312\python.exe`, `C:\Python313\python.exe`,
		"python", "python3", "py")
	for _, c := range cands {
		if c == n.Python {
			continue
		}
		if pythonUsable(c) {
			old := n.Python
			n.Python = c
			return n, fmt.Sprintf("解释器换成能用的：%s → %s", old, c)
		}
	}
	return n, "找不到任何能用的 Python 3 解释器（装了 Python 3 再启动，或设 COREHOST_NOTES_PYTHON 指过去）"
}

// notesPortOwner 8848 上是谁（不是我们的库时要说清楚）
func notesPortOwner(port int) (int, string) {
	owners := listenOwners()
	pid := owners[port]
	if pid == 0 {
		return 0, ""
	}
	names := processNames()
	return pid, names[pid]
}

// hostCoreDir 主进程的 data 根目录（启动时记一次；进程内唯一）。
// ensureNotes 是包级函数、拿不到 serveState，而"起不来说明"要和 host-start-failed.txt 放一起，
// 所以用一个包级变量记着它。
var hostCoreDir string

// notesPythonOverride 自愈换过的解释器（包级：ensureNotes 改不了 serveState）。
// 换了之后每次巡检都用它，直到进程重启。
var notesPythonOverride string

// coreDirOf 取主进程 data 根目录（拿不到就退回库自己的目录，至少别写不出来）
func coreDirOf(n notesSpec) string {
	if hostCoreDir != "" {
		return hostCoreDir
	}
	return filepath.Dir(n.Dir)
}

// writeNotesStartFailed 把"库为什么起不来"写成一份人能读、壳也能读的文件。
//
// 收 (coreDir, notesSpec) 而不是 serveState：**ensureNotes 是包级函数**（没有 st），
// 2026-10-10 我第一版写成方法，编译时才发现它调不到。
func writeNotesStartFailed(coreDir string, n notesSpec, reason string, extra string) {
	f := filepath.Join(coreDir, "data", "notes-start-failed.txt")
	_ = os.MkdirAll(filepath.Dir(f), 0o755)

	var b strings.Builder
	fmt.Fprintf(&b, "库起不来 / 起不稳：%s\n时间：%s\n\n", reason, time.Now().Format("2006-01-02 15:04:05"))

	fmt.Fprintf(&b, "库目录：%s\n启动脚本：%s（%s）\n解释器：%s\n端口：%d\n\n",
		n.Dir, n.script(), existsStr(n.script()), n.Python, n.Port)

	if pid, name := notesPortOwner(n.Port); pid != 0 {
		fmt.Fprintf(&b, "⚠ 端口 %d 已经被占：pid=%d（%s）\n"+
			"   如果这不是枢阁的库，请把它关掉；如果是上一次没退干净的库：taskkill /PID %d /F\n\n",
			n.Port, pid, name, pid)
	}

	if miss := notesProbeModulesCached(n.Python); len(miss) > 0 {
		pips := pipNamesOf(miss)
		soft := []string{}
		hard := []string{}
		for i, m := range miss {
			if m == "torch" || m == "sentence_transformers" {
				soft = append(soft, pips[i]) // 向量/模型：只提示（辞玖 2026-10-10 留后续）
			} else {
				hard = append(hard, pips[i])
			}
		}
		fmt.Fprintf(&b, "缺的 Python 包（import 名）：%s\n", strings.Join(miss, "、"))
		if len(hard) > 0 {
			fmt.Fprintf(&b, "   建议装：\"%s\" -m pip install %s\n", n.Python, strings.Join(hard, " "))
		}
		if len(soft) > 0 {
			fmt.Fprintf(&b, "   向量/检索相关（缺了会降级，本轮不自动装）：%s\n", strings.Join(soft, " "))
		}
		b.WriteString("\n")
	}

	if tail := notesTail(filepath.Join(n.Dir, "data", "logs", "notes.out.log"), 25); tail != "" {
		b.WriteString("库自己的最后几行输出（notes.out.log 尾部）：\n")
		b.WriteString(tail)
		b.WriteString("\n")
	}
	if extra != "" {
		b.WriteString("补充：" + extra + "\n")
	}
	b.WriteString("\n（这份文件由主进程生成；修好后它会自动消失）\n")

	_ = os.WriteFile(f, []byte(b.String()), 0o644)
}

func existsStr(p string) string {
	if _, err := os.Stat(p); err == nil {
		return "在"
	}
	return "**不在**"
}

// tailFile 读文件尾部 n 行（读不到就返回空）
func notesTail(path string, n int) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	lines := strings.Split(strings.TrimRight(string(b), "\n"), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}

// clearNotesStartFailed 库健康时把那份"起不来"的说明删掉（不留误导）
func clearNotesStartFailed(coreDir string) {
	_ = os.Remove(filepath.Join(coreDir, "data", "notes-start-failed.txt"))
}

// notesDiag 给 /api/host/status 用：一屏看清"库为什么不在"
func (st *serveState) notesDiag() map[string]any {
	n := st.notes
	out := map[string]any{
		"python":    n.Python,
		"script":    n.script(),
		"script_ok": func() bool { _, err := os.Stat(n.script()); return err == nil }(),
		"python_ok": pythonUsable(n.Python),
	}
	if pid, name := notesPortOwner(n.Port); pid != 0 {
		out["port_owner_pid"] = pid
		out["port_owner_name"] = name
	}
	if miss := notesProbeModulesCached(n.Python); len(miss) > 0 {
		out["missing_modules"] = miss
	}
	f := filepath.Join(st.coreDir, "data", "notes-start-failed.txt")
	if _, err := os.Stat(f); err == nil {
		out["start_failed_file"] = f
		out["start_failed_tail"] = notesTail(f, 12)
	}
	return out
}

var _ = net.Dial // 保留 net（其它文件用得到，避免以后删 import 时来回改）

// warmNotesDiag 启动后台预热一次依赖探针（不阻塞；主进程一上来就能答"缺什么"）。
func (st *serveState) warmNotesDiag() { go notesProbeModulesCached(st.notes.Python) }
