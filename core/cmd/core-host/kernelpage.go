//go:build windows

// 本文件：内核没在跑时，/core/ 给的**降级页**（顶部同一条小控制面板 + 空主体），
// 以及把 web/kernelctl.js 直接由主进程从磁盘发出去 ——
// 这样"内核在"与"内核不在"两种情况下，那条面板用的是同一份 JS。
//
// 为什么要降级页：内核界面是内核自己发的，内核一停它就打不开；
// 而启停按钮恰恰要在那时候能用（辞玖 2026-09-20：内核页最顶上就是启停）。
package main

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

const kernelDownHTML = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>枢阁 · 内核</title>
<style>
:root { --bg:#16161e; --panel:#1a1b26; --panel2:#1f2335; --panel3:#24283b; --bar:#20233a; --line:#3b4261; --line2:#2f3549; --fg:#c0caf5; --fg2:#9aa5ce; --dim:#565f89; --blue:#7aa2f7; --green:#9ece6a; --red:#f7768e; --amber:#e0af68; --purple:#bb9af7; --sel:#2d3350; --hover:#232433; --hit:#20222f; --edge:#101018;  --bg2:#1c1d2b; --bg3:#2a2e42; --sel2:#22304d; --sel3:#3d4670; --info-bg:#2a3a5c; --ok-bg:#1f3d2b; --ok-bg2:#233024; --warn-bg:#4d3a1f; --warn-bg2:#4a3b1f; --warn-bg3:#2a271d; --bad-bg:#3a2a30; --ok-bg3:#3d4a2a; --ok-bg4:#4b6a2f; --dim2:#8b93b8; --fg3:#a9b1d6; --cyan:#7dcfff; --blue2:#89b4fa; --pink:#f7a8b8; --red2:#d99; --magenta:#e8a; --amber2:#ffd479; --brown:#6b4a2f; --brown2:#6b5a2f; --green2:#2f6b43; --ok-fg:#e6ffd8; --primary:#2b3f66; --primary-line:#3d5a8a; --hover2:#262a45; --warn-fg2:#e0c07a; --warn-bg4:#3a3320; --line3:#2a2f45; --bg4:#15161f; --bg5:#1f2233; --on-primary:#c0caf5;}

  *{box-sizing:border-box}
  body{margin:0;background:var(--panel);color:var(--fg);font:14px/1.6 ui-sans-serif,"Microsoft YaHei",system-ui,sans-serif}
  .topbar{display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--bg);border-bottom:1px solid var(--line2)}
  .brand{font-size:15px;font-weight:bold}
  .pill{padding:2px 10px;border-radius:999px;font-size:12px;background:var(--panel3);color:var(--fg2);border:1px solid var(--line);text-decoration:none}
  .pill.blue{color:var(--blue);border-color:rgba(122,162,247,.35)}
  .kpanel{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:8px 14px;background:var(--panel2);border-bottom:1px solid var(--line2)}
  .kp-title{font-size:13px;font-weight:600}
  .kp-tag{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--fg2)}
  .kp-tag.ok{color:var(--green);border-color:var(--green)55}
  .kp-tag.bad{color:var(--red);border-color:var(--red)55}
  .kp-meta{font-size:12px;color:var(--dim2)}
  .kp-btn{font-size:12px;padding:5px 12px;border-radius:6px;border:1px solid var(--line);background:var(--panel3);color:var(--fg2);cursor:pointer}
  .kp-btn.main{background:var(--blue);color:var(--panel);border:none;font-weight:bold}
  .kp-btn.stop{background:var(--red);color:var(--panel);border:none;font-weight:bold}
  .kp-btn.off{color:var(--red);border-color:var(--red)55}
  .kp-note{font-size:12px;color:var(--green)}
  .empty{color:var(--dim);text-align:center;padding:80px 20px}
/* shuge-theme */
html[data-theme="light"] {
  --bg: #FBF6E7;
  --panel: #FFFCF3;
  --panel2: #FEF8EA;
  --panel3: #F5EDD9;
  --bar: #F7F0DE;
  --line: #E2D8BC;
  --line2: #EDE6D2;
  --fg: #33302A;
  --fg2: #4C473C;
  --dim: #6E6656;
  --blue: #2F6DB0;
  --green: #37703E;
  --red: #A8463F;
  --amber: #8A6110;
  --purple: #6A4FA3;
  --sel: #EFE3C2;
  --hover: #F6EEDA;
  --hit: #F8F2E2;
  --edge: #F3EAD3;
  --bg2: #F4EBD6;
  --bg3: #F1E7CF;
  --sel2: #DCE7F5;
  --sel3: #C9D8EE;
  --info-bg: #DCE8F7;
  --ok-bg: #DDEBD9;
  --ok-bg2: #DDEBD9;
  --warn-bg: #F6E7C6;
  --warn-bg2: #F6E7C6;
  --warn-bg3: #F7EAD0;
  --bad-bg: #F5DEDC;
  --ok-bg3: #E2EFD9;
  --ok-bg4: #D5E8CE;
  --dim2: #6E6656;
  --fg3: #3E3A31;
  --cyan: #1F6E8C;
  --blue2: #2F6DB0;
  --pink: #A8463F;
  --red2: #A8463F;
  --magenta: #8A3F7A;
  --amber2: #8A6110;
  --brown: #7A5A20;
  --brown2: #7A5A20;
  --green2: #37703E;
  --ok-fg: #2F5A33;
  --primary: #D7E5F8;
  --primary-line: #5E8CBE;
  --hover2: #F1E7CF;
  --warn-fg2: #7A5A20;
  --warn-bg4: #F6E7C6;
  --line3: #E2D8BC;
  --bg4: #F2E8D2;
  --bg5: #F1E7CF;
  --on-primary: #1F4E79;
}
/* shuge-theme */
button.theme-btn {
  display: inline-flex; align-items: center; gap: 6px;
  background: var(--panel3); color: var(--dim2);
  border: 1px solid var(--line2); border-radius: 999px;
  padding: 4px 12px; font-size: 12px; line-height: 1.5; cursor: pointer;
  white-space: nowrap;
}
button.theme-btn .ti { font-size: 13px; line-height: 1; opacity: .95; }
button.theme-btn .tt { color: var(--dim2); }
button.theme-btn:hover:not(:disabled) {
  color: var(--fg2); border-color: var(--line); background-image: none; box-shadow: none;
}
button.theme-btn:active:not(:disabled) { transform: translateY(1px) scale(.98); animation: none; }
button.theme-btn:focus-visible { outline: 2px solid var(--primary-line); outline-offset: 1px; }
@media (prefers-reduced-motion: reduce) { button.theme-btn:active:not(:disabled) { transform: none; } }
@media (prefers-reduced-motion: reduce) { button.theme-btn:active:not(:disabled) { transform: none; } }
@media (prefers-reduced-motion: reduce) { button.theme-btn:active:not(:disabled) { transform: none; } }
</style>
</head>
<body data-kdown="1">
  <div class="topbar">
    <span class="brand">🏛 枢阁 · 内核</span>
  <button id="btnTheme" class="theme-btn" title="主题：浅白·护眼（带一点暖黄，看着不刺眼）/ 深色；三个界面一起变，选择会记住" onclick="__shugeTheme.toggle()"><span class="ti">☀️</span><span class="tt">浅白·护眼</span></button>
    <span style="flex:1"></span>
    <a class="pill blue" href="/">🏛 控制台</a>
    <a class="pill blue" href="/notes/">📚 库</a>
  </div>
  <div class="kpanel">
    <span class="kp-title">🔧 内核</span>
    <span class="kp-tag" id="kpState">读取中…</span>
    <span class="kp-meta" id="kpMeta"></span>
    <span style="flex:1"></span>
    <button class="kp-btn" id="kpToggle" onclick="kpAct('toggle')">…</button>
    <button class="kp-btn" onclick="kpAct('restart')">⟳ 重启内核</button>
    <button class="kp-btn" id="kpAuto" onclick="kpToggleAuto()">自动重连：…</button>
    <span class="kp-note" id="kpNote"></span>
  </div>
  <div class="empty" id="kpBody">内核未运行</div>
  <script src="/kernelctl.js"></script>










<script>/* shuge-theme */
/* shuge-theme —— 枢阁的统一主题引导（浅白·护眼 / 深色）
 *
 * 辞玖 2026-10-06：「所有页面主题统一」+「图片中的问题是只页签能切换主题，其他地方没变」。
 *
 * 为什么要有这个文件（而不是各页面各写一小段）：
 *   · 三个界面（主程序控制台 8790 / 内核控制台 8795 / 库 8848）**端口不同 = 不同源**，
 *     localStorage 各存各的 → 必须有一份**服务端的偏好**；
 *   · 主程序（8790）是唯一地址，它的 「/api/ui/theme」 就是那份真相（本机来源放行，见 isLocalOrigin）；
 *   · 「只页签变了、其他地方没变」是因为库/内核是**同页 iframe**，外层改主题不会动到 iframe 里的文档
 *     → 切换时**主动推进 iframe**（同源直接写它的 <html>，跨端口再 postMessage 一次）；
 *   · 被别页嵌着时（主程序页嵌在内核控制台左栏）**跟随父页面**，并且不吃掉父页面的设置。
 *
 * 规则（避免互相打架/死循环）：
 *   · 只有**用户点击**才写 localStorage / POST 服务端 / 推给子 iframe / 通知同源标签页；
 *   · 从服务端、父页面、postMessage / BroadcastChannel 来的主题一律 「apply(t, true)」：只应用，不再回推
 *     （2026-10-06 事故：这里原来无条件 push，两个页面无限互推 →「两个主题自己频繁变化」）；
 *   · **实时**（辞玖 2026-10-06「全实时」）：连主程序（8790）的 SSE 地址 /api/ui/theme/stream，
 *     谁一改立刻推给所有页面；拿不到 SSE 就退回每 3 秒轮询（保底，不赌）；
 *   · 同一个源开多个标签页时，BroadcastChannel 立刻同步（storage 事件在某些情况下不投递，只当兜底）。
 */
(function () {
  var KEY = 'shuge-theme';
  var API = 'http://127.0.0.1:8790/api/ui/theme';   // 主程序（唯一地址）
  var STREAM = API + '/stream';                      // 同一条路的 SSE：主题一变就推过来（全实时）
  var POLL_MS = 3000;                                // 拿不到 SSE 时的保底轮询间隔（只在页面可见时跑）
  var USE_API = true;                                // 拿不到就静默退回 localStorage

  function norm(t) { return (t === 'light') ? 'light' : 'dark'; }

  // 按钮文字：两个 span（图标 + 文字），别用 textContent 整块覆盖（否则 icon 一起被抹掉）
  function label(t) {
    var b = document.getElementById('btnTheme');
    if (!b) return;
    var ic = b.querySelector ? b.querySelector('.ti') : null;
    var tx = b.querySelector ? b.querySelector('.tt') : null;
    var on = (t === 'light');
    if (ic) ic.textContent = on ? '🌙' : '☀️';
    if (tx) tx.textContent = on ? '深色' : '浅白·护眼';
    if (!ic && !tx) b.textContent = on ? '🌙 深色' : '☀️ 浅白·护眼';   // 老页面（没有两个 span）兜底
    b.title = on ? '当前浅白·护眼，点一下换深色（三个界面一起变）' : '当前深色，点一下换浅白·护眼（三个界面一起变）';
  }

  function frames() {
    var out = [];
    try {
      var fs = document.querySelectorAll('iframe');
      for (var i = 0; i < fs.length; i++) out.push(fs[i]);
    } catch (e) {}
    return out;
  }

  // 推给子 iframe（库 / 内核就是同页 iframe）+ 通知父页面
  function push(t) {
    frames().forEach(function (f) {
      try {
        var d = f.contentDocument;                  // 同源：直接写它的 <html>
        if (d) {
          d.documentElement.setAttribute('data-theme', t);
          var w = f.contentWindow;
          if (w && w.__shugeTheme && w.__shugeTheme.apply) w.__shugeTheme.apply(t, true);
        }
      } catch (e) {}                                 // 跨源拿不到 document：忽略，下面用 postMessage
      try { f.contentWindow.postMessage({ shugeTheme: t }, '*'); } catch (e) {}
    });
    try { if (window.parent && window.parent !== window) window.parent.postMessage({ shugeTheme: t }, '*'); } catch (e) {}
  }

  // 同一个**源**的别的页面（比如 8790 开两个标签）：BroadcastChannel 立刻通知它们。
  // ⚠️ 实测（2026-10-06，Edge 153 无头）：跨"端口"的标签**收不到** BroadcastChannel，localStorage 也各存各的
  //   —— 因为 8790 / 8795 / 8848 端口不同 = 三个不同的源。所以跨端口那一半只能靠**服务端**：
  //   点的那一页 POST 服务端，别的页面在自己**重新可见（切回来）**时拉一次（下面有 visibilitychange）。
  var chan = null;
  try { if (typeof BroadcastChannel !== 'undefined') chan = new BroadcastChannel('shuge-theme'); } catch (e) {}
  function tell(t) {
    try { if (chan) chan.postMessage({ shugeTheme: t }); } catch (e) {}
  }

  function save(t) {
    if (!USE_API) return;
    try {
      fetch(API, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ theme: t })
      }).catch(function () {});
    } catch (e) {}
  }

  function pull(then) {
    if (!USE_API) return;
    try {
      fetch(API, { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d && d.theme) then(norm(d.theme)); })
        .catch(function () {});
    } catch (e) {}
  }

  // 实时同步：SSE 为主，轮询兜底。两条路都只调 apply(t, true)（收到别人的主题不再往外推）
  function live() {
    if (!USE_API) return;
    var lastAsk = 0;
    function ask() {                                 // 兜底：直接问一次服务端（带 1.5 秒节流）
      var now = Date.now();
      if (now - lastAsk < 1500) return;
      lastAsk = now;
      pull(function (remote) { if (norm(remote) !== get()) apply(remote, true); });
    }
    var es = null;
    try { if (typeof EventSource !== 'undefined') es = new EventSource(STREAM); } catch (e) { es = null; }
    if (es) {
      es.addEventListener('theme', function (ev) {
        try {
          var d = JSON.parse(ev.data);
          if (d && d.theme && norm(d.theme) !== get()) apply(d.theme, true);
        } catch (e) {}
      });
      // EventSource 自己会重连；重连成功时再对一次表，断线期间漏掉的那次不能丢
      es.addEventListener('hello', function (ev) {
        try {
          var d = JSON.parse(ev.data);
          if (d && d.theme && norm(d.theme) !== get()) apply(d.theme, true);
        } catch (e) {}
      });
      es.addEventListener('error', function () { setTimeout(ask, 800); });
    } else {
      setInterval(function () { if (!document.hidden) ask(); }, POLL_MS);
    }
    // 切回这个页面时对一次表（SSE 没连上/断线时它最有用）
    try {
      document.addEventListener('visibilitychange', function () { if (!document.hidden) ask(); });
    } catch (e) {}
  }

  function apply(t, fromElsewhere) {
    t = norm(t);
    document.documentElement.setAttribute('data-theme', t);
    label(t);
    // ⚠️ 只有"用户点的那一下"才往外写/往外推。
    // 2026-10-06 事故：这里原来无条件 push(t)，于是收到推送的一方又推回去 —— 两个页面无限互推，
    // 值不一致时就表现为「两个主题自己频繁变化」。收到别人的主题只在本页生效，链路到此为止。
    if (!fromElsewhere) {
      try { localStorage.setItem(KEY, t); } catch (e) {}
      save(t);
      push(t);
      tell(t);
    }
    return t;
  }

  function get() { return document.documentElement.getAttribute('data-theme') || 'dark'; }

  window.__shugeTheme = {
    get: get,
    apply: apply,
    set: function (t) { return apply(t, false); },
    toggle: function () { return apply(get() === 'light' ? 'dark' : 'light', false); },
    push: push,
    api: API
  };

  // 别的页面（父页面 / 内核控制台里嵌着我们）推过来的主题
  window.addEventListener('message', function (ev) {
    var d = ev && ev.data;
    if (!d || !d.shugeTheme) return;
    if (norm(d.shugeTheme) === get()) return;        // 值没变：什么都不做（幂等短路，也少一次 DOM 写）
    apply(d.shugeTheme, true);
  });

  // 同源标签页里改的主题：BroadcastChannel 为主，localStorage 的 storage 事件兜底
  try {
    if (chan) chan.addEventListener('message', function (ev) {
      var d = ev && ev.data;
      if (d && d.shugeTheme && norm(d.shugeTheme) !== get()) apply(d.shugeTheme, true);
    });
  } catch (e) {}
  window.addEventListener('storage', function (ev) {
    if (ev && ev.key === KEY && ev.newValue && norm(ev.newValue) !== get()) apply(ev.newValue, true);
  });

  function parentTheme() {
    try {
      if (window.parent && window.parent !== window && window.parent.document) {
        return window.parent.document.documentElement.getAttribute('data-theme') || '';
      }
    } catch (e) {}                                   // 跨源父页面：读不到，靠 postMessage
    return '';
  }

  function hideIfEmbedded() {
    var embedded = false;
    try { embedded = (window.self !== window.top); } catch (e) { embedded = true; }
    if (embedded) {
      var b = document.getElementById('btnTheme');
      if (b) b.style.display = 'none';               // 外层页签栏上已经有一个了，别重复
    }
  }

  function local() { try { return localStorage.getItem(KEY) || ''; } catch (e) { return ''; } }
  function query() {
    try { return new URLSearchParams(location.search).get('theme') || ''; } catch (e) { return ''; }
  }

  function boot() {
    // 先用本页能立刻拿到的：?theme= → localStorage → 父页面 → 默认深色
    // （boot 这一步**不写**服务端：否则一个默认深色的新页面会把别人设的浅色冲掉）
    apply(query() || local() || parentTheme() || 'dark', true);
    // 再问服务端（唯一真相）：拿到了就覆盖
    pull(function (remote) { apply(remote, true); });
    live();                                          // 之后就是实时的：SSE 一推就变
    hideIfEmbedded();
  }

  if (typeof document === 'undefined') return;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();</script>
</body>
</html>
`

// writeKernelDown 出内核降级页
func writeKernelDown(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write([]byte(kernelDownHTML))
}

// handleKernelCtlJS 把 web/kernelctl.js 从磁盘直接发出去（走主进程，不经过内核）。
// 好处：内核在不在，那条面板的 JS 都是同一份。
func (st *serveState) handleKernelCtlJS(w http.ResponseWriter, r *http.Request) {
	path := filepath.Join(st.coreDir, "web", "kernelctl.js")
	b, err := os.ReadFile(path)
	if err != nil {
		http.Error(w, "kernelctl.js not found: "+err.Error(), http.StatusNotFound)
		return
	}
	w.Header().Set("Content-Type", "application/javascript; charset=utf-8")
	w.Header().Set("Cache-Control", "no-cache")
	if strings.Contains(r.URL.RawQuery, "v=") { // 带版本参数时允许长缓存（暂未用，留个口子）
		w.Header().Set("Cache-Control", "public, max-age=3600")
	}
	_, _ = w.Write(b)
}

// handlePanelJS 发面板装载器（web/page-panels.js）。
//
// 2026-10-10（辞玖定 B 方案）：库页/内核页**不再是 iframe** —— 由这个脚本把那一页的 HTML
// 取回来、装配进面板（样式搬过去、脚本重建）。直接从磁盘读，改它不用重编主进程。
func (st *serveState) handlePanelJS(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/javascript; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	b, err := os.ReadFile(filepath.Join(st.coreDir, "web", "page-panels.js"))
	if err != nil {
		http.Error(w, "读不到 web/page-panels.js："+err.Error(), http.StatusInternalServerError)
		return
	}
	_, _ = w.Write(b)
}
