//go:build windows

// 本文件：**设置**这一页（2026-10-11 辞玖：「1 做（设置页）」）。
//
// 为什么要独立一页、而不是弹个菜单：
//   · 菜单只能放"点一下就跑"的动作；设置里还有**状态**（当前主题、库在不在、路径是哪、什么版本），
//     这些得有地方显示；
//   · 窗口版与网页版共用同一页：页面自己能做的（主题、看状态）两边都能用；
//     只有窗口才有的（缩放/全屏/开发者工具/在浏览器打开/打开目录）问壳要**桥**，
//     没有桥就整段隐身 —— 不留"点了没反应"的按钮。
//
// 它被装进主程序页的面板里（#panelSettings），跟库页/内核页一个路子；
// 单独访问 http://127.0.0.1:8790/settings/ 也能看（此时没有壳，窗口那一组自动隐身）。
package main

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

const settingsMount = "/settings"

const settingsPageHTML = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>枢阁 · 设置</title>
</head>
<body>
<div class="setgrid">

  <div class="setcard">
    <h3>外观</h3>
    <div class="srow">
      <div class="st"><b>主题</b><i>浅白·护眼是暖黄底、不刺眼；深色适合夜里。三个界面（控制台 / 库 / 内核）一起变，选完记住。</i></div>
      <div class="sc">
        <span class="seg" id="setTheme">
          <button data-act="theme" data-t="light"><span class="ti">☀️</span><span class="tt">浅白·护眼</span></button>
          <button data-act="theme" data-t="dark"><span class="ti">🌙</span><span class="tt">深色</span></button>
        </span>
      </div>
    </div>
  </div>

  <div class="setcard">
    <h3>这台机器上</h3>
    <div class="facts" id="setFacts"><div class="fact"><span class="fk">读取中</span><span class="fv">…</span></div></div>
  </div>

  <div class="setcard">
    <h3>回收站</h3>
    <div class="srow">
      <div class="st"><b>删除与覆盖留下的前像</b><i>库里的删除、以及"保存回原格式"覆盖前的那一版都留在这里，可以恢复回来。</i></div>
      <div class="sc"><button class="setbtn" data-act="openBin">打开回收站</button></div>
    </div>
  </div>

  <div id="setShellOnly" style="display:none">
    <div class="setcard">
      <h3>窗口</h3>
      <div class="srow">
        <div class="st"><b>缩放</b><i>整页放大缩小；快捷键 Ctrl+加号 / Ctrl+减号 / Ctrl+0 回 100%。</i></div>
        <div class="sc">
          <button class="setbtn" data-act="zoomOut">缩小 −</button>
          <button class="setbtn" data-act="zoomIn">放大 ＋</button>
          <button class="setbtn" data-act="zoomReset">100%</button>
        </div>
      </div>
      <div class="srow">
        <div class="st"><b>全屏</b><i>整屏看内容（F11 切换，Esc 退出）。</i></div>
        <div class="sc"><button class="setbtn" data-act="toggleFullscreen">全屏 / 退出</button></div>
      </div>
    </div>

    <div class="setcard">
      <h3>排查与打开</h3>
      <div class="srow">
        <div class="st"><b>刷新界面</b><i>界面卡住或刚改过东西时用；快捷键 F5。</i></div>
        <div class="sc"><button class="setbtn" data-act="reload">刷新</button></div>
      </div>
      <div class="srow">
        <div class="st"><b>开发者工具</b><i>排查界面问题用（F12）。</i></div>
        <div class="sc"><button class="setbtn" data-act="openDevTools">打开</button></div>
      </div>
      <div class="srow">
        <div class="st"><b>在浏览器打开</b><i>用系统默认浏览器打开同一页（对比网页版时用）。</i></div>
        <div class="sc"><button class="setbtn" data-act="openInBrowser">打开</button></div>
      </div>
      <div class="srow">
        <div class="st"><b>打开平台 data 目录</b><i>日志、账本、主题与端口文件都在这里。</i></div>
        <div class="sc"><button class="setbtn" data-act="openDataDir">打开</button></div>
      </div>
      <div class="srow">
        <div class="st"><b>打开库目录</b><i>笔记正文所在的目录。</i></div>
        <div class="sc"><button class="setbtn" data-act="openVaultDir">打开</button></div>
      </div>
    </div>
  </div>

  <div class="setcard">
    <h3>关于枢阁</h3>
    <div class="facts" id="setAbout"><div class="fact"><span class="fk">读取中</span><span class="fv">…</span></div></div>
  </div>

  <div class="setcard" id="setBridgeNote" style="display:none">
    <h3>窗口功能</h3>
    <div class="note">缩放、全屏、开发者工具这些是**窗口版**才有的；你现在是在浏览器里打开这一页，所以它们不显示。用桌面那个「枢阁」窗口打开就有。</div>
  </div>

</div>
</body>
</html>
`

// handleSettingsMount GET /settings → /settings/
func (st *serveState) handleSettingsMount(w http.ResponseWriter, r *http.Request) {
	http.Redirect(w, r, settingsMount+"/", http.StatusFound)
}

// handleSettings GET /settings/ —— 设置这一页
func (st *serveState) handleSettings(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	if !strings.HasSuffix(r.URL.Path, "/") {
		http.Redirect(w, r, settingsMount+"/", http.StatusFound)
		return
	}
	_, _ = w.Write([]byte(settingsPageHTML))
}

// handleShellState GET /shell-state.json —— 桌面壳写的小状态文件（桥 / 缩放 / 壳版本 / 界面地址）。
// 网页版没有这个文件，回空对象（设置页据此显示"网页版"）。
func (st *serveState) handleShellState(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	b, err := os.ReadFile(filepath.Join(st.coreDir, "data", "shell-state.json"))
	if err != nil {
		_, _ = w.Write([]byte("{}"))
		return
	}
	_, _ = w.Write(b)
}

// handleFavicon GET /favicon.ico —— 一个内联的小图标（主进程自己发，别让浏览器每次都 404）。
//
// 为什么不放个文件：这一页的"图标"就一个方块 + 一个「枢」字，内联最省事，
// 也省得为它维护一个二进制资源（辞玖 2026-10-11：清掉最后那条 404）。
func (st *serveState) handleFavicon(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "image/svg+xml; charset=utf-8")
	w.Header().Set("Cache-Control", "public, max-age=86400")
	_, _ = w.Write([]byte(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">` +
		`<rect width="64" height="64" rx="14" fill="#F7F0DE"/>` +
		`<rect x="1.5" y="1.5" width="61" height="61" rx="12.5" fill="none" stroke="#E2D8BC" stroke-width="3"/>` +
		`<text x="32" y="45" font-size="38" text-anchor="middle" fill="#1F4E79" ` +
		`font-family="Microsoft YaHei,Segoe UI,sans-serif">枢</text></svg>`))
}
