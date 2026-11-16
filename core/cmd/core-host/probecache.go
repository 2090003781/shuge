//go:build windows

// 本文件：状态采样的短缓存（辞玖 2026-09-20："缓存可以引入"）。
//
// 为什么要有它：控制台页面每 3 秒拉一次 /api/host/status，而那个接口会去问库（HTTP 健康接口）与内核。
// 页面多开几个、或者脚本一轮轮打，库就得一次次答话 —— 库是单线程服务，探针不该成为它的负担。
// 缓存只做一件事：把"同一小段时间里的多次询问"合并成**一次真实探测**，不改语义。
//
// 代价是状态会"最多 TTL 秒旧"，所以状态接口把**采样时刻**一起给出（页面上显示"采样 N 秒前"），
// 不藏着 —— 看到的就是真实采到的，只是取样时间写在旁边。
package main

import (
	"encoding/json"
	"net/http"
	"sync"
	"time"
)

const (
	// TTL 必须**比页面轮询间隔长**才吃得住重复：页面 3 秒一轮，所以两个都给 5 秒。
	// 代价是状态最多旧 5 秒 —— 所以页面上如实写"采样 N 秒前"，不藏。
	// 需要更跟手就把这里调小（内核单价约 1.8ms，本身也很便宜）。
	notesHealthTTL = 5 * time.Second // 库的健康采样（库是单线程，最该省）
	coreStatusTTL  = 5 * time.Second // 内核状态采样
)

// probeCache 一个按 TTL 收敛的采样器：TTL 内复用上次结果，过期才真去探测。
type probeCache[T any] struct {
	mu       sync.Mutex
	ttl      time.Duration
	at       time.Time
	val      T
	inflight chan struct{} // 非 nil = 有人正在探测
}

func newProbeCache[T any](ttl time.Duration) *probeCache[T] {
	return &probeCache[T]{ttl: ttl}
}

// get 取一次采样（TTL 内直接复用），返回结果与**这次结果的采样时刻**。
//
// 探测**不持锁**（2026-09-20 审计发现：库的 /api/health 实测最慢到 5~10 秒，
// 而它以前是在锁里调的 → 那几秒内所有 /api/host/status 请求一起排队卡住）：
//
//	· TTL 内 → 直接给缓存；
//	· 有人正在探 → 有旧值就先给旧值（立刻返回，不陪等）；冷启动没旧值才等这一轮；
//	· 没人探 → 自己探，探完再更新（同一时刻只有一个探测在跑，不会打出一堆）。
func (c *probeCache[T]) get(probe func() T) (T, time.Time) {
	c.mu.Lock()
	if !c.at.IsZero() && time.Since(c.at) < c.ttl {
		v, at := c.val, c.at
		c.mu.Unlock()
		return v, at
	}
	if ch := c.inflight; ch != nil {
		if !c.at.IsZero() {
			v, at := c.val, c.at
			c.mu.Unlock()
			return v, at
		}
		c.mu.Unlock()
		<-ch // 冷启动且已有别人在探：等这一轮（探测本身有超时，有界）
		c.mu.Lock()
		v, at := c.val, c.at
		c.mu.Unlock()
		return v, at
	}
	ch := make(chan struct{})
	c.inflight = ch
	c.mu.Unlock()

	v := probe() // 锁外探测：再慢也不挡别人读状态

	c.mu.Lock()
	c.val, c.at = v, time.Now()
	c.inflight = nil
	c.mu.Unlock()
	close(ch)
	return v, c.at
}

var (
	notesHealthProbe = newProbeCache[bool](notesHealthTTL)
	// 端口上有没有人在监听（netstat）：只在 TCP 那一下连不上时才用，TTL 短
	notesListenerProbe = newProbeCache[bool](2 * time.Second)
	coreStatusProbe  = newProbeCache[coreStatus](coreStatusTTL)
)

// coreStatus 内核状态的采样结果
type coreStatus struct {
	Online bool   `json:"online"`
	PID    int    `json:"pid,omitempty"`
	Seq    uint64 `json:"seq,omitempty"`
	UpSec  int64  `json:"uptime_s,omitempty"`
}

// probeCoreStatus 真去问一次内核（/healthz）
func (st *serveState) probeCoreStatus() coreStatus {
	cs := coreStatus{}
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get(st.url)
	if err != nil {
		return cs
	}
	defer resp.Body.Close()
	var h struct {
		PID    int   `json:"pid"`
		Uptime int64 `json:"uptime_s"`
		Ledger struct {
			LastSeq uint64 `json:"last_seq"`
		} `json:"ledger"`
	}
	if json.NewDecoder(resp.Body).Decode(&h) == nil {
		cs = coreStatus{Online: true, PID: h.PID, UpSec: h.Uptime, Seq: h.Ledger.LastSeq}
	}
	return cs
}
