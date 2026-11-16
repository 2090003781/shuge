// 本文件：轮转的回归测试（2026-09-12 补测试网）。
//
// 钉住两条今天踩过的契约：
//  1. 轮转后 **跨文件 seq 连续**（轮转不能丢号/重号）——这是账本作为"唯一真相源"的底线
//  2. **开局即轮转不再静默**：上一个文件超阈值时新建文件，必须计数 + 能取出 RotateInfo
//     （原来默默新建，状态与账本里都看不到"轮转发生过"）
package ledger

import (
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"

	"core/internal/plugin"
)

func quietLog() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// 造一条平台事件（realm=platform 才入总账）
func ev(typ string, n int) plugin.Event {
	return plugin.NewEvent(typ, "test", map[string]any{"n": n, "pad": "xxxxxxxxxxxxxxxxxxxx"})
}

// ① 轮转触发 + 跨文件 seq 连续
func TestRotationKeepsSeqContinuous(t *testing.T) {
	dir := t.TempDir()
	l, err := Open(Options{Dir: dir, MaxFileBytes: 2 << 10, Logger: quietLog(), CommitEvery: 10 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 200; i++ {
		l.Append(ev("platform.test.pad", i))
	}
	l.Flush()
	st := l.Stats()
	if st.Rotations == 0 {
		t.Fatalf("写了 200 条、阈值 2KB，应该发生轮转：%+v", st)
	}
	_ = l.Close()

	rep, err := Verify(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(rep.Files) < 2 {
		t.Fatalf("轮转后应有多个文件：%d", len(rep.Files))
	}
	// 跨文件边界必须衔接（rep.Gaps 非空即断号）
	for _, g := range rep.Gaps {
		t.Fatalf("轮转处出现 seq 缺口：after=%d before=%d 缺 %d", g.AfterSeq, g.BeforeSeq, g.Missing)
	}
	if len(rep.Disorder) != 0 {
		t.Fatalf("轮转不该产生重复/回退：%+v", rep.Disorder)
	}
}

// ② 开局即轮转：可续写文件已超阈值 → 计数 + 能取出 RotateInfo（回归今天的修复）
func TestRotationAtOpenIsReported(t *testing.T) {
	dir := t.TempDir()
	// 先正常写满一个文件（阈值 2KB）
	l1, err := Open(Options{Dir: dir, MaxFileBytes: 2 << 10, Logger: quietLog(), CommitEvery: 10 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 60; i++ {
		l1.Append(ev("platform.test.pad", i))
	}
	l1.Flush()
	_ = l1.Close()

	// 再用**更小**的阈值打开：最新文件必然超阈值 → 开局即轮转
	l2, err := Open(Options{Dir: dir, MaxFileBytes: 512, Logger: quietLog(), CommitEvery: 10 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = l2.Close() }()

	info, ok := l2.TakeOpenRotation()
	if !ok {
		t.Fatal("开局即轮转必须能被取出（否则轮转在状态与账本里都看不见）")
	}
	if info.Old == "" || info.New == "" || info.Bytes <= 0 {
		t.Fatalf("RotateInfo 信息不全：%+v", info)
	}
	if info.Old == info.New {
		t.Fatalf("新旧文件不该同名：%+v", info)
	}
	if st := l2.Stats(); st.Rotations == 0 {
		t.Fatalf("开局即轮转也要计数：%+v", st)
	}
	// 取一次就清空（避免重复补发事件）
	if _, again := l2.TakeOpenRotation(); again {
		t.Fatal("RotateInfo 应取一次即清")
	}
}

// ③ 非 platform 域的事件不入总账（辞玖定：本期只收平台自身；演练时踩过这个点）
func TestNonPlatformRealmIsSkipped(t *testing.T) {
	dir := t.TempDir()
	l, err := Open(Options{Dir: dir, Logger: quietLog(), CommitEvery: 10 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	before := l.Stats().Written
	l.Append(plugin.NewEvent("drill.custom.event", "drill", map[string]any{"n": 1}))
	l.Flush()
	if got := l.Stats().Written; got != before {
		t.Fatalf("非 platform 域事件不该入总账：written %d → %d", before, got)
	}
	_ = l.Close()
}

// ④ 末尾半行恢复：崩溃留下的半行应被截断而不是废掉整个文件
func TestRecoverTruncatesHalfLine(t *testing.T) {
	dir := t.TempDir()
	l, err := Open(Options{Dir: dir, Logger: quietLog(), CommitEvery: 10 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	l.Append(ev("platform.test.pad", 1))
	l.Flush()
	path := filepath.Join(dir, l.Stats().File)
	_ = l.Close()

	f, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = f.WriteString(`{"seq":999,"type":"platform.test.half`) // 半行
	_ = f.Close()

	l2, err := Open(Options{Dir: dir, Logger: quietLog(), CommitEvery: 10 * time.Millisecond})
	if err != nil {
		t.Fatalf("半行不该导致打不开：%v", err)
	}
	defer func() { _ = l2.Close() }()
	if l2.Stats().TruncatedBytes == 0 {
		t.Fatal("半行应被截断并记入 TruncatedBytes")
	}
	rep, err := Verify(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(rep.BadLines) != 0 {
		t.Fatalf("截断后不该留坏行：%+v", rep.BadLines)
	}
}
