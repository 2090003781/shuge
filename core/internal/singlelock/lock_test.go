// 本文件：单实例锁的回归测试（2026-09-12 补测试网）。
//
// 为什么单测这一条：它是"两个 core 同时写账本"的唯一防线（2026-09-12 真实事故），
// 而它的行为（同进程重复获取、释放后可再获取）平时没人会手工验。
package singlelock

import "testing"

// 获取后 Busy 为真；释放后为假；重复获取应报错（同一进程内也算"已被持有"）
func TestAcquireExclusiveAndBusy(t *testing.T) {
	const name = "singlelock-test-core"

	l, err := Acquire(name)
	if err != nil {
		t.Fatalf("首次获取应成功：%v", err)
	}
	if !Busy(name) {
		t.Fatal("已持有时 Busy 应为真（看门狗就是靠它判断旧实例是否退干净）")
	}
	if _, err2 := Acquire(name); err2 == nil {
		t.Fatal("重复获取应报错（否则两个 core 会同时写账本）")
	}

	l.Release()
	if Busy(name) {
		t.Fatal("释放后 Busy 应为假")
	}
	// 释放后应能再次获取（重启路径依赖它）
	l2, err := Acquire(name)
	if err != nil {
		t.Fatalf("释放后应能重新获取：%v", err)
	}
	l2.Release()
}

// 不同的名字互不影响（平台用不同的名字区分不同锁）
func TestDifferentNamesDoNotBlock(t *testing.T) {
	a, err := Acquire("singlelock-test-a")
	if err != nil {
		t.Fatal(err)
	}
	defer a.Release()
	b, err := Acquire("singlelock-test-b")
	if err != nil {
		t.Fatalf("不同名字不该互相阻塞：%v", err)
	}
	b.Release()
}
