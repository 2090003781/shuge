//go:build windows

// 本文件：内核降级页的回归（2026-09-20 改版）。
//
// 为什么值得单测：内核界面是内核自己发的，内核一停它就打不开 —— 而"启动内核"按钮恰恰要在那时能用。
// 所以降级页必须带**同一批面板 id**、带 “data-kdown“ 标记、并且引入**同一份** kernelctl.js
// （两条路径共用一份 JS，避免"能启动"和"不能启动"两套实现漂移）。
package main

import (
	"strings"
	"testing"
)

func TestKernelDownPageHasPanel(t *testing.T) {
	for _, id := range []string{`id="kpState"`, `id="kpMeta"`, `id="kpToggle"`, `id="kpAuto"`, `id="kpNote"`} {
		if !strings.Contains(kernelDownHTML, id) {
			t.Errorf("降级页缺少面板元素 %s", id)
		}
	}
	if !strings.Contains(kernelDownHTML, `data-kdown="1"`) {
		t.Error("降级页该带 data-kdown 标记（kernelctl.js 靠它决定：内核起来后自动换成真界面）")
	}
	if !strings.Contains(kernelDownHTML, `src="/kernelctl.js"`) {
		t.Error("降级页该引入 /kernelctl.js（与内核界面同一份）")
	}
	if !strings.Contains(kernelDownHTML, "启动内核") && !strings.Contains(kernelDownHTML, "kpAct('toggle')") {
		t.Error("降级页该有启停按钮")
	}
}
