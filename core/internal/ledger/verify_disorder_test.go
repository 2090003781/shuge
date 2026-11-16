// 本文件：校验器对"并发写坏"的回归测试。
//
// 背景（真实事故，2026-09-12）：控制台"重启"时旧 core 的 HTTP 已停、进程还在收尾，
// 新实例就打开了账本 → 两边各自从文件末尾取 seq 起点 → 924/925 被写两次。
// 当时校验器只报"缺口 925→937"，看不出真正病因（丢记录 ≠ 两个写者）。
// 现在：seq 重复/回退必须单独报出来。
package ledger

import (
	"os"
	"path/filepath"
	"testing"
)

func TestVerifyDetectsDuplicateSeq(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, ledgerPrefix+"20260912-000000.jsonl")
	content := "" +
		`{"seq":1,"ts":"2026-09-12T11:50:11+08:00","type":"platform.core.started","source":"core"}` + "\n" +
		`{"seq":2,"ts":"2026-09-12T11:50:11+08:00","type":"platform.module.started","source":"bridge"}` + "\n" +
		// 第二个写者从 seq 1 重新开始 —— 这正是事故当天的形态
		`{"seq":1,"ts":"2026-09-12T11:50:13+08:00","type":"platform.core.stopped","source":"core"}` + "\n"
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	rep, err := Verify(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(rep.Disorder) != 1 {
		t.Fatalf("应报到 1 处 seq 重复/回退，实际 %d 处：%+v", len(rep.Disorder), rep.Disorder)
	}
	if rep.Disorder[0].Seq != 1 || rep.Disorder[0].PrevSeq != 2 || rep.Disorder[0].Line != 3 {
		t.Fatalf("重复点定位不对：%+v", rep.Disorder[0])
	}
	if rep.OK {
		t.Fatal("有重复 seq 时 OK 必须为 false")
	}
}

func TestVerifyCleanFileHasNoDisorder(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, ledgerPrefix+"20260912-000000.jsonl")
	content := "" +
		`{"seq":1,"ts":"2026-09-12T11:50:11+08:00","type":"a","source":"core"}` + "\n" +
		`{"seq":2,"ts":"2026-09-12T11:50:12+08:00","type":"b","source":"core"}` + "\n"
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	rep, err := Verify(dir)
	if err != nil {
		t.Fatal(err)
	}
	if !rep.OK || len(rep.Disorder) != 0 || len(rep.Gaps) != 0 {
		t.Fatalf("干净文件应 OK：%+v", rep)
	}
}
