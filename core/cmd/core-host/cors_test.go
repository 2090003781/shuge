//go:build windows

// 本文件：CORS / 跨源写请求的回归（2026-09-30 体检）。
//
// 背景：宿主（8790）原来无条件发 `Access-Control-Allow-Origin: *`，且所有启停接口没有鉴权。
// 后果：**主人浏览器里随便一个网页**就能读到 /api/host/*，还能 POST /api/notes/stop、
// /api/core/restart、/api/host/power/off 把枢阁关掉（简单跨源 POST 不触发预检）。
//
// 纪律（这三条就是修复的全部内容）：
//
//	① 没 Origin（curl / 插件 / 同源导航）→ 照旧放行；
//	② Origin 是本机（127.0.0.1 / localhost / [::1]，端口不限）→ 放行并**回显该来源**；
//	③ 别的来源 → 读请求放行但**不给 ACAO**（别的网页读不到），写请求直接 403。
package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestIsLocalOrigin(t *testing.T) {
	local := []string{
		"http://127.0.0.1:8790", "http://127.0.0.1:3080", "http://localhost:8790",
		"http://LOCALHOST:1234", "http://[::1]:8790", "https://127.0.0.1",
	}
	for _, o := range local {
		if !isLocalOrigin(o) {
			t.Errorf("本机来源应放行：%s", o)
		}
	}
	foreign := []string{
		"https://evil.example.com", "http://192.168.1.9:8790", "http://127.0.0.1.evil.com",
		"http://localhost.evil.com", "null", "://bad", "",
	}
	for _, o := range foreign {
		if isLocalOrigin(o) {
			t.Errorf("外来来源不该放行：%s", o)
		}
	}
}

func do(method, origin string) *httptest.ResponseRecorder {
	hit := false
	h := cors(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hit = true
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("inner"))
	}))
	req := httptest.NewRequest(method, "http://127.0.0.1:8790/api/notes/stop", nil)
	if origin != "" {
		req.Header.Set("Origin", origin)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if origin != "" && !isLocalOrigin(origin) && method != http.MethodGet && method != http.MethodOptions {
		if hit {
			panic("跨源写请求不该到达内层处理器")
		}
	}
	return rec
}

func TestCorsMatrix(t *testing.T) {
	// ① 没 Origin：放行，且不发 ACAO（同源/curl 不需要）
	r := do("POST", "")
	if r.Code != http.StatusOK {
		t.Errorf("无 Origin 的 POST 应放行，得到 %d", r.Code)
	}
	if got := r.Header().Get("Access-Control-Allow-Origin"); got != "" {
		t.Errorf("无 Origin 时不该发 ACAO，得到 %q", got)
	}

	// ② 本机 Origin：放行 + 回显（不是 *）
	r = do("POST", "http://127.0.0.1:8790")
	if r.Code != http.StatusOK {
		t.Errorf("本机 Origin 的 POST 应放行，得到 %d", r.Code)
	}
	if got := r.Header().Get("Access-Control-Allow-Origin"); got != "http://127.0.0.1:8790" {
		t.Errorf("本机 Origin 应回显，得到 %q", got)
	}

	// ③ 外来 Origin + 写：403，且内层不许被执行
	r = do("POST", "https://evil.example.com")
	if r.Code != http.StatusForbidden {
		t.Errorf("跨源 POST 应 403，得到 %d", r.Code)
	}
	if got := r.Header().Get("Access-Control-Allow-Origin"); got != "" {
		t.Errorf("跨源不该给 ACAO，得到 %q", got)
	}

	// ④ 外来 Origin + 读：放行但不给 ACAO（内容读不到）
	r = do("GET", "https://evil.example.com")
	if r.Code != http.StatusOK {
		t.Errorf("跨源 GET 应放行（只是不给 ACAO），得到 %d", r.Code)
	}
	if got := r.Header().Get("Access-Control-Allow-Origin"); got != "" {
		t.Errorf("跨源 GET 不该给 ACAO，得到 %q", got)
	}

	// ⑤ 外来 Origin 的预检：204，但不给 ACAO
	r = do("OPTIONS", "https://evil.example.com")
	if r.Code != http.StatusNoContent {
		t.Errorf("预检应 204，得到 %d", r.Code)
	}
	if got := r.Header().Get("Access-Control-Allow-Origin"); got != "" {
		t.Errorf("跨源预检不该给 ACAO，得到 %q", got)
	}
}
