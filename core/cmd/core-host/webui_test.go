//go:build windows

// 本文件：内核界面（web/index.html + web/app.js）的静态一致性回归。
//
// 为什么值得单测（2026-09-20 踩的坑）：app.js 的视图映射里留了一个**已经从 index.html 删掉**的视图，
// 初始化那次 go('notes') 里 getElementById 拿到 null → 直接抛错 → 后面的 tickConsole() 没跑，
// 界面上表现为"插件列表根本没加载出来"。这种错在浏览器里只表现为"某一坨没出来"，
// 静态看代码很难发现，所以这里把它钉死：**映射里的每个 id 必须在 index.html 里真实存在**，
// 而且默认落地的那个视图必须存在且是 active。
package main

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

var webPairRe = regexp.MustCompile(`([A-Za-z]+):\s*'([^']+)'`)

func TestWebViewMappingsExist(t *testing.T) {
	htmlB, err := os.ReadFile(filepath.Join("..", "..", "web", "index.html"))
	if err != nil {
		t.Skipf("读不到 web/index.html：%v", err)
	}
	jsB, err := os.ReadFile(filepath.Join("..", "..", "web", "app.js"))
	if err != nil {
		t.Skipf("读不到 web/app.js：%v", err)
	}
	html, js := string(htmlB), string(jsB)

	for _, m := range regexp.MustCompile(`const (VIEWS|NAVS) = \{([^}]*)\}`).FindAllStringSubmatch(js, -1) {
		name, body := m[1], m[2]
		pairs := webPairRe.FindAllStringSubmatch(body, -1)
		if len(pairs) == 0 {
			t.Errorf("%s 里一对映射都没解析出来：%q", name, body)
		}
		for _, p := range pairs {
			key, id := p[1], p[2]
			if !strings.Contains(html, `id="`+id+`"`) {
				t.Errorf("%s[%s] → %q，但 index.html 里没有这个元素 —— 点了它会抛错、把后面的加载全带崩", name, key, id)
			}
		}
	}

	// 默认落地页：app.js 里 `|| 'console'` 的那个 'console' 必须是存在的视图，且默认带 active
	if !strings.Contains(js, `|| 'console'`) {
		t.Error("app.js 的默认视图不是 console 了？改了 index.html 的视图就同步看一眼这里")
	}
	if !strings.Contains(html, `id="view-console"`) {
		t.Error("index.html 里缺 view-console")
	}
	// 默认 active 的那个 view 必须是 console（否则首屏一片空）
	activeRe := regexp.MustCompile(`<div class="view active" id="([^"]+)"`)
	m := activeRe.FindStringSubmatch(html)
	if m == nil {
		t.Error("index.html 里找不到默认 active 的视图")
	} else if m[1] != "view-console" {
		t.Errorf("默认 active 的视图是 %q，期望 view-console", m[1])
	}
}
