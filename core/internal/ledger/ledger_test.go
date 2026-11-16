// 本文件：账本回归测试——把 2026-09-11 晚上的三个真实故障钉成用例。
//
// 被钉住的故障：
//
//	① 断电/强杀后账本尾部半行 → 必须截断且 seq 续号（不能重号、不能留脏行）
//	② 那 4h26m 的"记账黑洞" → verify 必须能把序号缺口报出来（不能沉默）
//	③ 写盘失败 → 应急文件 + 计数 + degraded + critical 进积压（不能静默丢）
package ledger

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"core/internal/plugin"
)

func testEvent(typ string) plugin.Event {
	return plugin.Event{Type: typ, Source: "test", Time: time.Now(), Scope: []string{"platform"}}
}

// TestSeqContinuesAfterHalfLineCrash 半行截断 + seq 续号
func TestSeqContinuesAfterHalfLineCrash(t *testing.T) {
	dir := t.TempDir()
	l, err := Open(Options{Dir: dir, CommitEvery: 2 * time.Millisecond})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	for i := 0; i < 5; i++ {
		l.Append(testEvent("platform.core.started"))
	}
	l.Flush()
	if err := l.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	// 模拟断电：尾部追加半行（无换行）
	files, err := ListLedgerFiles(dir)
	if err != nil || len(files) == 0 {
		t.Fatalf("no ledger files: %v", err)
	}
	f, err := os.OpenFile(files[len(files)-1], os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatalf("open for half line: %v", err)
	}
	if _, err := f.WriteString(`{"seq":999,"id":"HALF-LINE-SHOULD-BE-TRUNCATED`); err != nil {
		t.Fatalf("write half line: %v", err)
	}
	_ = f.Close()

	// 重开：应截断半行并续号
	l2, err := Open(Options{Dir: dir, CommitEvery: 2 * time.Millisecond})
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	if got := l2.Stats().TruncatedBytes; got == 0 {
		t.Fatal("应报告截断字节数（半行未截断）")
	}
	l2.Append(testEvent("platform.core.started"))
	l2.Flush()
	_ = l2.Close()

	rep, err := Verify(dir)
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if !rep.OK {
		t.Fatalf("不该有缺口/坏行: %s", rep.Summary)
	}
	if rep.Lines != 6 {
		t.Fatalf("应为 6 条（半行不计入），实际 %d", rep.Lines)
	}
	if rep.LastSeq != 6 {
		t.Fatalf("seq 应从 5 续到 6，实际 %d", rep.LastSeq)
	}
	if strings.Contains(readAllFiles(t, dir), "HALF-LINE") {
		t.Fatal("半行内容应被截断丢弃")
	}
}

// TestVerifyDetectsGap 序号缺口必须被报出（"丢没丢"的唯一判据）
func TestVerifyDetectsGap(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, ledgerPrefix+"20260911-000000.jsonl")
	lines := `{"seq":1,"ts":"2026-09-11T00:00:00+08:00","type":"platform.core.started","realm":"platform"}` + "\n" +
		`{"seq":2,"ts":"2026-09-11T00:00:01+08:00","type":"platform.core.started","realm":"platform"}` + "\n" +
		`{"seq":5,"ts":"2026-09-11T00:00:02+08:00","type":"platform.core.started","realm":"platform"}` + "\n"
	if err := os.WriteFile(path, []byte(lines), 0o644); err != nil {
		t.Fatalf("seed: %v", err)
	}
	rep, err := Verify(dir)
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if rep.OK {
		t.Fatal("有缺口却报 OK（沉默丢账）")
	}
	if len(rep.Gaps) != 1 || rep.Gaps[0].Missing != 2 {
		t.Fatalf("缺口应为 1 处、缺 2 条，实际 %+v", rep.Gaps)
	}
}

// TestWriteFailureDegrades 写盘失败 → 应急文件 + 计数 + degraded + critical 积压不丢
func TestWriteFailureDegrades(t *testing.T) {
	dir := t.TempDir()
	l, err := Open(Options{Dir: dir, CommitEvery: 2 * time.Millisecond})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer l.Close()

	l.faultWrite = func([]byte) error { return errors.New("disk full (injected)") }
	l.Append(testEvent("platform.process.exited")) // critical：必须进积压
	l.Append(testEvent("platform.plugin.registered"))
	l.Flush()

	st := l.Stats()
	if st.WriteErrors == 0 {
		t.Fatal("应记录写盘失败次数")
	}
	if !st.Degraded {
		t.Fatal("应进入 degraded 状态")
	}
	if st.CriticalBacklog == 0 {
		t.Fatal("关键事件应进积压（不丢）")
	}
	if st.DroppedBusiness == 0 {
		t.Fatal("业务事件应按阶梯丢弃并计数")
	}
	if m, _ := filepath.Glob(filepath.Join(dir, emergencyPrefix+"*.jsonl")); len(m) == 0 {
		t.Fatal("应写应急文件（留痕，不静默）")
	}

	// 故障恢复后：积压的关键事件应补写，账本仍连续无缺口
	l.faultWrite = nil
	l.Flush()
	rep, err := Verify(dir)
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if !rep.OK {
		t.Fatalf("恢复后不该有缺口: %s", rep.Summary)
	}
	if rep.Lines != 1 {
		t.Fatalf("只剩关键事件（业务事件已按阶梯丢弃），实际 %d 条", rep.Lines)
	}
}

// TestUnknownPlatformTypeCounted 漏登记的平台事件必须被计数（catalog 缺口的可见化）
func TestUnknownPlatformTypeCounted(t *testing.T) {
	dir := t.TempDir()
	l, err := Open(Options{Dir: dir, CommitEvery: 2 * time.Millisecond})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer l.Close()
	l.Append(testEvent("platform.not.registered.yet"))
	l.Flush()
	if got := l.Stats().UnknownTypes["platform.not.registered.yet"]; got == 0 {
		t.Fatal("未登记的平台事件类型应被计数")
	}
}

// TestForeignRealmSkipped 外部域事件本期不入账（但也不该算作"漏登记"）
func TestForeignRealmSkipped(t *testing.T) {
	dir := t.TempDir()
	l, err := Open(Options{Dir: dir, CommitEvery: 2 * time.Millisecond})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer l.Close()
	if got := l.AppendResult(testEvent("clock.tick")); got != ResultSkipped {
		t.Fatalf("外部域事件应跳过，实际 %v", got)
	}
	l.Flush()
	if st := l.Stats(); len(st.UnknownTypes) != 0 {
		t.Fatalf("外部域不该计入漏登记: %+v", st.UnknownTypes)
	}
}

func readAllFiles(t *testing.T, dir string) string {
	t.Helper()
	files, _ := ListLedgerFiles(dir)
	var sb strings.Builder
	for _, f := range files {
		b, err := os.ReadFile(f)
		if err != nil {
			t.Fatalf("read %s: %v", f, err)
		}
		sb.Write(b)
	}
	return sb.String()
}
