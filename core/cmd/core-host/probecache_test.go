//go:build windows

// 本文件：采样缓存的两条纪律（2026-09-20 加缓存时立的回归）——
//
//	① TTL 内**复用**、过期**必须重探**（否则状态会永远停在第一次采样）
//	② 日志尾巴按 (size, mtime) 认版本：文件没变就不读盘；变了必须重读
package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestProbeCacheTTL(t *testing.T) {
	calls := 0
	c := newProbeCache[int](250 * time.Millisecond)
	probe := func() int { calls++; return calls }

	v1, at1 := c.get(probe)
	v2, at2 := c.get(probe)
	if calls != 1 {
		t.Errorf("TTL 内应当只探一次，实际探了 %d 次", calls)
	}
	if v1 != 1 || v2 != 1 {
		t.Errorf("TTL 内应当拿到同一个值：%v / %v", v1, v2)
	}
	if at1.IsZero() || !at1.Equal(at2) {
		t.Errorf("同一份采样应当带同一个采样时刻：%v / %v", at1, at2)
	}

	time.Sleep(300 * time.Millisecond)
	v3, at3 := c.get(probe)
	if calls != 2 || v3 != 2 {
		t.Errorf("过期必须重新探：calls=%d v3=%v", calls, v3)
	}
	if !at3.After(at1) {
		t.Errorf("重探后采样时刻要前进：%v → %v", at1, at3)
	}
}

func TestTailOfCachesBySizeAndMtime(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "x.log")
	if err := os.WriteFile(p, []byte("a\nb\nc\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	stt, _ := os.Stat(p)
	mtime := stt.ModTime()

	l1, ok := tailOf(p, 2)
	if !ok || len(l1) != 2 || l1[0] != "b" || l1[1] != "c" {
		t.Fatalf("首次读取不对：%v", l1)
	}

	// 换成同样大小、并把 mtime 改回去 —— 缓存认的就是这两个，应当**不重读**
	if err := os.WriteFile(p, []byte("X\nY\nZ\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(p, mtime, mtime); err != nil {
		t.Fatal(err)
	}
	l2, _ := tailOf(p, 2)
	if len(l2) != 2 || l2[0] != "b" || l2[1] != "c" {
		t.Errorf("(size, mtime) 没变时应当走缓存，实际拿到 %v", l2)
	}

	// 把 mtime 动一下 —— 必须重读，看到新内容
	if err := os.Chtimes(p, mtime.Add(2*time.Second), mtime.Add(2*time.Second)); err != nil {
		t.Fatal(err)
	}
	l3, _ := tailOf(p, 2)
	if len(l3) != 2 || l3[0] != "Y" || l3[1] != "Z" {
		t.Errorf("文件变了必须重读，实际拿到 %v", l3)
	}
}
