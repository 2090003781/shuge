//go:build windows

// 本文件：库保活的两条纪律（2026-09-20 提速时立的回归）——
//
//	① **端口优先**：端口开着 = 库在跑 → 立刻返回，绝不去打 HTTP。
//	   为什么单测它：库是单线程服务，"探针"必须便宜；老逻辑每拍都打 /api/health，
//	   库卡住时要等 8 秒超时，而巡检现在 1 秒一拍 —— 那样会把巡检串成一条慢链。
//	② **维护窗口**：只有 /api/notes/stop（维护入口）与「关闭枢阁」开窗口；
//	   窗口过了必须自动恢复（库永远在线，不认「人工停止」那套）。
package main

import (
	"net"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"
)

func TestNotesHoldWindow(t *testing.T) {
	notesHoldClear()
	if notesHeld() {
		t.Fatal("默认不该在维护窗口里")
	}
	notesHoldFor(300 * time.Millisecond)
	if !notesHeld() {
		t.Fatal("开了窗口就该判为 held")
	}
	time.Sleep(400 * time.Millisecond)
	if notesHeld() {
		t.Fatal("窗口过了必须自动恢复：库永远在线，没有「人工停止」这回事")
	}
	notesHoldClear()
}

// TestEnsureNotesPortFirst 端口开着（但故意不答 HTTP）时：立刻返回、不拉进程、不打 HTTP。
func TestEnsureNotesPortFirst(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	// 只收连接、永不回包：模拟"库在忙（首次加载模型 / 大批重建索引）"
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			_ = c // 不回任何东西
		}
	}()

	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "src"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "src", "server.py"), []byte("# stub\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	n := notesSpec{Dir: dir, Python: filepath.Join(dir, "no-such-python.exe"), Port: port,
		URL: "http://127.0.0.1:" + strconv.Itoa(port)}

	notesHoldClear()
	t0 := time.Now()
	spawned := ensureNotes(n, "单测", false)
	el := time.Since(t0)
	if spawned {
		t.Errorf("库已经在跑（端口开着），不该拉进程")
	}
	if el > 1*time.Second {
		t.Errorf("端口优先应当立刻返回，实测 %v —— 说明它又去打 HTTP 了（老逻辑要等 8 秒超时）", el)
	}
	out, _ := os.ReadFile(filepath.Join(dir, "data", "logs", "notes.out.log"))
	if len(out) > 0 {
		t.Errorf("不该有拉起动作的记录：%q", string(out))
	}
}

// TestNoteExitLifetime 退避只针对"连续短命"，不惩罚偶发的一次被杀。
func TestNoteExitLifetime(t *testing.T) {
	notesShortLivedMu.Lock()
	notesShortLived = 0
	notesShortLivedMu.Unlock()

	if d := noteExitLifetime(60 * time.Second); d != 0 {
		t.Errorf("活够一分钟的退出该立刻重拉，得到退避 %v", d)
	}
	if d := noteExitLifetime(1 * time.Second); d != 0 {
		t.Errorf("偶发短命（第 1 次）该立刻重拉，得到退避 %v", d)
	}
	if d := noteExitLifetime(1 * time.Second); d != 0 {
		t.Errorf("第 2 次也该立刻重拉，得到退避 %v", d)
	}
	if d := noteExitLifetime(1 * time.Second); d == 0 {
		t.Errorf("连续第 3 次短命才该退避")
	}
	if d := noteExitLifetime(1 * time.Second); d != 0 {
		t.Errorf("退避后计数该归零，又该立刻重拉，得到 %v", d)
	}
}
