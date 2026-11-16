//go:build windows

// 本文件：把**内核界面**挂到 /core/ 下（辞玖 2026-09-20：控制台 = 主进程页；
// 内核界面降成"内核自己的详细面" —— 组件列表、插件回收站、权限、账本都归它）。
//
//	GET /core       → 302 /core/
//	GET /core/...   → 反代到内核（8795），路径去掉 /core 前缀
//
// 内核不在时**不出浏览器错误页**，直接回控制台页（那里就有「启动内核」）。
package main

import (
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
)

const coreMount = "/core"

func (st *serveState) handleCoreMount(w http.ResponseWriter, r *http.Request) {
	http.Redirect(w, r, coreMount+"/", http.StatusFound)
}

func (st *serveState) handleCoreUI(w http.ResponseWriter, r *http.Request) {
	if !st.kernelAnswering() {
		// 内核没在跑：出「内核降级页」——顶部还是那条小面板（能直接点「启动内核」），
		// 起来了由 kernelctl.js 自己换成真正的内核界面。
		writeKernelDown(w)
		return
	}
	p := strings.TrimPrefix(r.URL.Path, coreMount)
	if !strings.HasPrefix(p, "/") {
		p = "/" + p
	}
	target, err := url.Parse(st.coreBase())
	if err != nil {
		http.Error(w, "内核地址不对："+err.Error(), http.StatusInternalServerError)
		return
	}
	proxy := httputil.NewSingleHostReverseProxy(target)
	orig := proxy.Director
	proxy.Director = func(req *http.Request) {
		orig(req)
		req.URL.Path = p // /core/app.js → /app.js
		req.URL.RawPath = ""
		req.Host = target.Host
	}
	proxy.FlushInterval = -1 // SSE（/api/events/stream）立即刷出
	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		writeKernelDown(w) // 转发中途内核掉了：回内核降级页（那儿能重新启动它）
	}
	proxy.ServeHTTP(w, r)
}
