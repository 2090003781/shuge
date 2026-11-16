//go:build windows

// 本文件：控制台页（page.go 的 hostPageHTML）的回归测试 —— 把页面里的内联脚本
// 丢进 node 的桩 DOM 里跑一遍，看它渲染出来的是什么。
//
// 为什么值得单测（2026-09-20 补）：巡检横条曾经**永远**显示「巡检状态读取失败（主进程在？）」——
// 不是接口挂了，而是 loadPatrol() 的成功分支里调了一个**页面里根本没定义**的 esc()，
// ReferenceError 被自己的 .catch 吞掉，界面上看起来像"网络失败"。
// 这类"帮手函数漏定义"的错，只有把脚本真跑一遍才看得见 —— 静态看代码看不出来。
//
// 这里钉三件事：
//
//	· up   模式：三段（整体生命/库/内核）都渲染、日志来源含「平台事件」、日志行真出来
//	· down 模式（内核不在 + 已人工关闭）：平台事件**根本不出现**（不是出现了再解释一句）
//	· 标签切换：默认在控制台；切到库/内核才加载对应 iframe；标签与视图的 active 跟着走；hash 跟着变
package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// harnessJS：桩 DOM + 假接口，把页面脚本跑一遍，把关注的元素吐成 JSON。
// 用法：node harness.mjs <page.js> <up|down>
// 注意：**故意不提供 esc()** —— 页面该自己带；少了它这个测试就该红。
const harnessJS = `
import fs from 'node:fs';
import vm from 'node:vm';

const script = fs.readFileSync(process.argv[2], 'utf8');
const mode = process.argv[3] || 'up';
// id 清单**由页面 HTML 解析得到**（调用方传进来）。
// 为什么不能手写：2026-09-20 的教训 —— 手写清单里留着 'note' 等一批页面里并不存在的 id，
// 于是页面里「取 note 元素」这种"元素不存在"的 bug 被桩自己补上了，测试反而成了共谋。
const ids = JSON.parse(process.argv[4] || '[]');
const els = {};
for (const id of ids) {
  els[id] = { id, textContent: '', innerHTML: '', className: id.startsWith('tab-') ? 'tab' : '', style: {},
    href: '', src: '', attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; this[k] = v; },
    getAttribute(k) { return this.attrs[k]; } };
}
if (els['tab-console']) els['tab-console'].className = 'tab active';
if (els['view-console']) els['view-console'].className = 'view active';

const PATROL = { verdict: 'ok', last_at: '2026-09-20T18:52:40+08:00', every_s: 300, total_runs: 2167,
  known: 1, problems: null, summary: '自检 19 项：18 通过 / 1 已知 / 0 警告 / 0 失败 / 0 跳过',
  transitions: [{ at: '2026-09-18T23:51:19+08:00', kind: 'recovered', summary: '恢复' }] };
const up = mode === 'up';
const STATUS = {
  core: up ? { online: true, pid: 49572, seq: 15454, uptime_s: 2952 } : { online: false },
  notes: { dir: 'D:\\core-notes', healthy: up, port_open: up, port: 8848, url: 'http://127.0.0.1:8848',
           checked_at: new Date(Date.now() - 3000).toISOString() },
  host_pid: 49768, uptime_s: 3000, port: 8790, host_state: 'healthy',
  core_checked_at: new Date(Date.now() - 3000).toISOString(),
  host_paused: !up, core_paused: false, core_autoreconnect: true, quarantine: false, lock_busy: false,
};

const payload = (u) => {
  if (u.startsWith('/api/host/logsources')) return { ok: true, kernel_up: up, sources: up
    ? [{ source: 'core', name: '内核', bytes: 4096, updated: '2026-09-20T18:52:40+08:00' }]
    : [{ source: 'host', name: '主进程', bytes: 2048, updated: '2026-09-20T18:52:40+08:00' }] };
  if (u.startsWith('/api/host/log?')) return { ok: true, found: true,
    lines: ['2026-09-20T18:52:40+08:00 | INFO | core | 控制台日志区一行'] };
  if (u.startsWith('/api/host/patrol')) return PATROL;
  if (u.startsWith('/api/host/status')) return STATUS;
  if (u.startsWith('/api/host/check')) return { summary: '全部通过', items: [{ status: 'ok', name: '账本连续性', detail: 'fine' }] };
  return {};
};

const errs = [];
const calls = [];
const sandbox = {
  document: { getElementById: (id) => els[id] || null, body: { className: '' } },
  location: { pathname: '/console', search: '', origin: 'http://127.0.0.1:8790', href: '', hash: '' },
  fetch: async (u) => { calls.push(String(u)); return { ok: true, json: async () => payload(String(u)) }; },
  confirm: () => false,
  setInterval: () => 0, setTimeout: () => 0, clearInterval: () => {}, addEventListener: () => {},
  Date, JSON, Math, String, parseInt, console,
};
sandbox.window = sandbox;   // 页面里有 window.selectLogSource / window.tickLogs / window.showTab
const ctx = vm.createContext(sandbox);
process.on('unhandledRejection', (e) => errs.push(String((e && e.message) || e)));
try {
  vm.runInContext(script, ctx, { filename: 'page.js' });
} catch (e) {
  errs.push('同步抛错: ' + e.message);
}
await new Promise((r) => setTimeout(r, 400));

const snap = () => ({
  libSrc: els.libFrame.getAttribute('src') || '',
  coreSrc: els.coreFrame.getAttribute('src') || '',
  tabConsole: els['tab-console'].className,
  tabNotes: els['tab-notes'].className,
  tabCore: els['tab-core'].className,
  viewConsole: els['view-console'].className,
  viewNotes: els['view-notes'].className,
  viewCore: els['view-core'].className,
  hash: sandbox.location.hash,
});

const initial = snap();
vm.runInContext("showTab('notes')", ctx);
await new Promise((r) => setTimeout(r, 50));
const toNotes = snap();
vm.runInContext("showTab('core')", ctx);
await new Promise((r) => setTimeout(r, 50));
const toCore = snap();
vm.runInContext("showTab('console')", ctx);
await new Promise((r) => setTimeout(r, 50));
const backConsole = snap();

// 点一次「关闭枢阁」：只验它会发请求、不抛错（真发出去也就只是 POST，服务端会真的关 ——
// 所以这里靠 confirm 返回 true + 桩 fetch，**不会真关**，因为桩 fetch 不会打到网络）
let poErr = null;
vm.runInContext("confirm = () => true", ctx);
try { vm.runInContext("powerOff()", ctx); } catch (e) { poErr = String((e && e.message) || e); }
await new Promise((r) => setTimeout(r, 120));
const powerOff = {
  posted: calls.filter((u) => u.indexOf('power/off') >= 0),
  noteText: els.note.textContent,
  err: poErr,
};

// 安全取值：id 清单是**页面里真实存在的那些**，页面没有的元素这里就是空（不再替它造）
const txt = (id) => (els[id] ? els[id].textContent : '');
const cls = (id) => (els[id] ? els[id].className : '');
const html = (id) => (els[id] ? els[id].innerHTML : '');

console.log(JSON.stringify({
  patrolClass: cls('patrolBar'),
  patrolHTML: html('patrolBar'),
  lifeTag: txt('lifeTag'),
  state: txt('state'),
  kids: html('kids'),
  hostPill: txt('hostPill'),
  logSources: html('logSources'),
  logStream: html('logStream'),
  dotNotes: txt('dotNotes'),
  dotCore: txt('dotCore'),
  initial, toNotes, toCore, backConsole, powerOff,
  errs,
}));
`

var scriptTagRe = regexp.MustCompile(`(?s)<script>(.*?)</script>`)

type tabSnap struct {
	LibSrc      string `json:"libSrc"`
	CoreSrc     string `json:"coreSrc"`
	TabConsole  string `json:"tabConsole"`
	TabNotes    string `json:"tabNotes"`
	TabCore     string `json:"tabCore"`
	ViewConsole string `json:"viewConsole"`
	ViewNotes   string `json:"viewNotes"`
	ViewCore    string `json:"viewCore"`
	Hash        string `json:"hash"`
}

type pageOut struct {
	PatrolClass string  `json:"patrolClass"`
	PatrolHTML  string  `json:"patrolHTML"`
	LifeTag     string  `json:"lifeTag"`
	State       string  `json:"state"`
	Kids        string  `json:"kids"`
	HostPill    string  `json:"hostPill"`
	LogSources  string  `json:"logSources"`
	LogStream   string  `json:"logStream"`
	DotNotes    string  `json:"dotNotes"`
	DotCore     string  `json:"dotCore"`
	Initial     tabSnap `json:"initial"`
	ToNotes     tabSnap `json:"toNotes"`
	ToCore      tabSnap `json:"toCore"`
	BackConsole tabSnap `json:"backConsole"`
	PowerOff    struct {
		Posted   []string `json:"posted"`
		NoteText string   `json:"noteText"`
		Err      string   `json:"err"`
	} `json:"powerOff"`
	Errs []string `json:"errs"`
}

// runPage 在 node 桩 DOM 里跑一遍页面脚本
func runPage(t *testing.T, mode string) pageOut {
	t.Helper()
	node, err := exec.LookPath("node")
	if err != nil {
		t.Skip("没装 node，跳过页面脚本冒烟")
	}
	m := scriptTagRe.FindStringSubmatch(hostPageHTML)
	if m == nil {
		t.Fatal("页面里找不到内联脚本")
	}
	dir := t.TempDir()
	js := filepath.Join(dir, "page.js")
	harness := filepath.Join(dir, "harness.mjs")
	if err := os.WriteFile(js, []byte(m[1]), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(harness, []byte(harnessJS), 0o644); err != nil {
		t.Fatal(err)
	}
	// 把页面里真实存在的 id 交给桩（**别手写**：手写清单会替页面补上它缺的元素，把 bug 抹掉）
	idRe := regexp.MustCompile(`id="([^"]+)"`)
	var ids []string
	for _, m := range idRe.FindAllStringSubmatch(hostPageHTML, -1) {
		ids = append(ids, m[1])
	}
	idsJSON, _ := json.Marshal(ids)
	out, err := exec.Command(node, harness, js, mode, string(idsJSON)).CombinedOutput()
	if err != nil {
		t.Fatalf("跑页面脚本失败：%v\n%s", err, out)
	}
	line := strings.TrimSpace(string(out))
	if i := strings.LastIndexByte(line, '\n'); i >= 0 {
		line = line[i+1:]
	}
	var got pageOut
	if err := json.Unmarshal([]byte(line), &got); err != nil {
		t.Fatalf("harness 输出没法解析：%v\n%s", err, out)
	}
	if len(got.Errs) > 0 {
		t.Errorf("页面脚本抛错了：%v", got.Errs)
	}
	return got
}

// TestHostPageConsoleRenders 三段（整体生命 / 库 / 内核）+ 日志区，内核在时
func TestHostPageConsoleRenders(t *testing.T) {
	got := runPage(t, "up")
	if strings.Contains(got.PatrolHTML, "读取失败") {
		t.Errorf("巡检横条还是失败文案：%q", got.PatrolHTML)
	}
	if got.PatrolClass != "patrol ok" {
		t.Errorf("巡检横条配色 = %q，想要 %q", got.PatrolClass, "patrol ok")
	}
	for _, want := range []string{"常驻巡检", "18 通过", "每 5 分钟"} {
		if !strings.Contains(got.PatrolHTML, want) {
			t.Errorf("巡检横条里没有 %q：%q", want, got.PatrolHTML)
		}
	}
	if got.State != "枢阁运行中" {
		t.Errorf("① 整体生命状态 = %q", got.State)
	}
	for _, want := range []string{"📚 库 🟢 在线", "🔧 内核", "自动重连：开"} {
		if !strings.Contains(got.Kids, want) {
			t.Errorf("① 下属摘要里没有 %q：%q", want, got.Kids)
		}
	}
	// 「关闭枢阁」：页面必须有 id="note"（这个 bug 的形态就是"元素不存在 → 点击抛错 → POST 不发"）
	if !strings.Contains(hostPageHTML, `id="note"`) {
		t.Error(`页面缺 id="note"：「关闭枢阁」点了会抛错、POST 根本发不出去`)
	}
	if got.PowerOff.Err != "" {
		t.Errorf("点「关闭枢阁」抛错了：%s", got.PowerOff.Err)
	}
	if len(got.PowerOff.Posted) != 1 || !strings.Contains(got.PowerOff.Posted[0], "/api/host/power/off") {
		t.Errorf("点「关闭枢阁」没有发出 POST：%v", got.PowerOff.Posted)
	}
	if got.PowerOff.NoteText == "" {
		t.Error("点「关闭枢阁」后状态行没有反馈")
	}

	// 控制台不再给库/内核各占一张卡片（辞玖 2026-09-20）；它们就是顶栏两个标签
	if strings.Contains(hostPageHTML, `id="notesCard"`) || strings.Contains(hostPageHTML, `id="coreCard"`) {
		t.Error("控制台不该再有「库 / 内核」两张卡片")
	}
	if !strings.Contains(got.Kids, "showTab(&#39;notes&#39;)") && !strings.Contains(got.Kids, "showTab('notes')") {
		t.Errorf("① 摘要该能点着切标签：%q", got.Kids)
	}
	// 标签上的状态点
	if got.DotNotes != "🟢" || got.DotCore != "🟢" {
		t.Errorf("标签状态点 = %q / %q，都该是 🟢", got.DotNotes, got.DotCore)
	}
	// ④ 日志区：来源列表 + 真渲染出日志行
	if !strings.Contains(got.LogSources, "全部（聚合时间线）") || !strings.Contains(got.LogSources, "平台事件") {
		t.Errorf("④ 日志来源列表不对：%q", got.LogSources)
	}
	if !strings.Contains(got.LogStream, "控制台日志区一行") {
		t.Errorf("④ 日志行没渲染出来：%q", got.LogStream)
	}
}

// TestHostPageTabsSwitch 三个标签：默认控制台；切到库/内核才加载 iframe；active 与 hash 跟着走
func TestHostPageTabsSwitch(t *testing.T) {
	got := runPage(t, "up")
	ini := got.Initial
	if !strings.Contains(ini.TabConsole, "active") || !strings.Contains(ini.ViewConsole, "active") {
		t.Errorf("默认该停在控制台：tab=%q view=%q", ini.TabConsole, ini.ViewConsole)
	}
	if ini.LibSrc != "" || ini.CoreSrc != "" {
		t.Errorf("没切过去就不该加载 iframe：lib=%q core=%q", ini.LibSrc, ini.CoreSrc)
	}
	if !strings.Contains(got.ToNotes.LibSrc, "/notes/") {
		t.Errorf("切到库没加载 /notes/：%q", got.ToNotes.LibSrc)
	}
	if !strings.Contains(got.ToNotes.TabNotes, "active") || !strings.Contains(got.ToNotes.ViewNotes, "active") {
		t.Errorf("切到库后 active 不对：tab=%q view=%q", got.ToNotes.TabNotes, got.ToNotes.ViewNotes)
	}
	if strings.Contains(got.ToNotes.ViewConsole, "active") {
		t.Errorf("切到库后控制台视图该收起：%q", got.ToNotes.ViewConsole)
	}
	if got.ToNotes.Hash != "#notes" {
		t.Errorf("hash 该变 #notes，实际 %q", got.ToNotes.Hash)
	}
	if !strings.Contains(got.ToCore.CoreSrc, "/core/") {
		t.Errorf("切到内核没加载 /core/：%q", got.ToCore.CoreSrc)
	}
	if !strings.Contains(got.ToCore.TabCore, "active") || !strings.Contains(got.ToCore.ViewCore, "active") {
		t.Errorf("切到内核后 active 不对：tab=%q view=%q", got.ToCore.TabCore, got.ToCore.ViewCore)
	}
	if !strings.Contains(got.BackConsole.ViewConsole, "active") || got.BackConsole.Hash != "#console" {
		t.Errorf("切回控制台不对：view=%q hash=%q", got.BackConsole.ViewConsole, got.BackConsole.Hash)
	}
	// 已经加载过的 iframe 不该被重置（避免每次切回来都重新加载一遍）
	if got.BackConsole.LibSrc != got.ToNotes.LibSrc {
		t.Errorf("库 iframe 被重置了：%q → %q", got.ToNotes.LibSrc, got.BackConsole.LibSrc)
	}
}

// TestHostPageConsoleDegradesQuietly 内核不在 + 人工关闭：该没有的就没有，不解释
func TestHostPageConsoleDegradesQuietly(t *testing.T) {
	got := runPage(t, "down")
	if strings.Contains(got.LogSources, "平台事件") {
		t.Errorf("内核不在时不该列出「平台事件」来源：%q", got.LogSources)
	}
	if !strings.Contains(got.State, "已人工关闭") {
		t.Errorf("① 整体生命状态 = %q，想要含「已人工关闭」", got.State)
	}
	if got.PatrolClass != "patrol ok" {
		t.Errorf("巡检横条不该受内核影响：%q", got.PatrolClass)
	}
	if !strings.Contains(got.Kids, "⛔ 未运行") {
		t.Errorf("① 下属摘要该显示内核未运行：%q", got.Kids)
	}
	if !strings.Contains(got.Kids, "📚 库 ⛔") {
		t.Errorf("① 下属摘要该显示库不在：%q", got.Kids)
	}
	if got.DotCore == "🟢" {
		t.Errorf("内核不在时标签状态点不该是 🟢：%q", got.DotCore)
	}
}
