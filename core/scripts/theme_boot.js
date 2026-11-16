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
})();
