// 枢阁 · 面板装载器（page-panels.js）
//
// 2026-10-10（辞玖定 B 方案）：**不再用 iframe** 装库页与内核页，改成"把那一页的 HTML 取回来装配进面板"。
//
// 三件事：
//   ① 装配：fetch 那一页 → DOMParser → 样式（裸 body 选择器改写成 #panelX 作用域）+ 元素 + **重建脚本**；
//   ② 只装一次，之后切换只是显示/隐藏；
//   ③ **接口转发**（关键）：库页/内核页原本各自跑在自己的端口上（8848 / 8795），
//      装进同一个文档后，它们那些相对路径的 fetch（`/api/...`）会打到宿主的根上 —— 404。
//      所以这里按"当前面板"给请求加前缀：库页 → `/notes`，内核页 → `/core`。
//      `/api/host/*` 与静态件（/kernelctl.js、/page-panels.js）不转发。
(function () {
  'use strict';

  var loaded = {};          // id -> true
  var active = null;        // 当前面板 id（决定请求往哪转发）

  // ⚠️ 两边都要**排除 /api/host/***：那是主进程自己的接口，本来就在根上（没有 /notes、/core 前缀）。
  //    2026-10-11 抓到：库面板里 /api/host/log?source=… 被加成了 /notes/api/host/log → 404，
  //    整棵树报「加载失败: bad json」（面板里的 api() 把非 JSON 的 404 页当成 bad json）。
  //    ⚠️ /api/core/* **同理**（2026-10-14 接手分析抓到）：内核的启停/重启/自动重连也是**宿主**
  //    自己的接口（serve.go:173-175 的 /api/core/start|stop|restart|autoreconnect），
  //    加前缀后被代理到内核（8795）→ 内核没这条路由 → 404。
  //    现象：窗口版内核面板状态灯正常（走 /api/core/status），但**启停按钮点了没反应**。
  var RULES = {
    panelNotes: ['/notes', function (p) { return !/^\/api\/(host|core)\//.test(p); }],  // 库：除宿主接口外都转发
    panelCore: ['/core', function (p) { return !/^\/api\/(host|core)\//.test(p); }]    // 内核：同上
  };

  function rewrite(path) {
    if (!active || !RULES[active]) return path;
    var rule = RULES[active];
    if (!/^\/api\//.test(path)) return path;   // 只管 API
    if (!rule[1](path)) return path;
    return rule[0] + path;
  }

  function rewriteUrl(input) {
    try {
      var abs = (typeof input === 'string') ? new URL(input, location.href) : input;
      if (!abs || abs.origin !== location.origin) return input;   // 跨源不动
      var np = rewrite(abs.pathname);
      if (np === abs.pathname) return input;
      var out = np + (abs.search || '') + (abs.hash || '');
      return (typeof input === 'string' && input[0] === '/') ? out : out;
    } catch (e) { return input; }
  }

  // ② 转发：包一层 fetch 与 XMLHttpRequest（装一次，按 active 动态判断）
  (function installForwarding() {
    if (window.__shugeForwarding) return;
    window.__shugeForwarding = true;

    var of = window.fetch;
    if (of) {
      window.fetch = function (input, init) {
        try { return of.call(this, rewriteUrl(input), init); }
        catch (e) { return of.call(this, input, init); }
      };
    }
    var oo = window.XMLHttpRequest && window.XMLHttpRequest.prototype.open;
    if (oo) {
      window.XMLHttpRequest.prototype.open = function (method, url) {
        var args = Array.prototype.slice.call(arguments);
        try { args[1] = rewriteUrl(url); } catch (e) {}
        return oo.apply(this, args);
      };
    }
  })();

  // 相对地址 → 按这一页自己的前缀拼绝对路径（2026-10-13：内核页的 app.js 就是这么坏的）
  //   "app.js" + "/core/" → "/core/app.js"；已经是绝对地址/协议相对的就不动。
  function absUrl(u, base) {
    u = String(u || '');
    if (!u) return u;
    if (/^(https?:)?\/\//.test(u)) return u;      // //cdn… 或 http(s)://…
    if (u.charAt(0) === '/') return u;              // 本来就是绝对路径
    if (/^(data:|blob:|javascript:)/i.test(u)) return u;
    base = base || '/';
    if (base.charAt(base.length - 1) !== '/') base += '/';
    return base + u;
  }

  function scopeCss(css, scopeSel) {
    // 只动"选择器里的裸 body"；不动 body 作为属性值/字符串出现的地方
    return String(css || '').replace(/(^|[},])(\s*)body(?=[\s,{:])/g, function (m, a, b) {
      return a + b + scopeSel;
    });
  }

  function adoptStyles(doc, host, scopeSel, base) {
    var nodes = doc.querySelectorAll('style, link[rel="stylesheet"]');
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (n.tagName.toLowerCase() === 'style') {
        var st = document.createElement('style');
        st.textContent = scopeCss(n.textContent, scopeSel);
        host.appendChild(st);
      } else {
        var lk = document.createElement('link');
        lk.rel = 'stylesheet';
        lk.href = absUrl(n.getAttribute('href'), base);
        host.appendChild(lk);
      }
    }
  }

  function adoptScripts(doc, host, base) {
    var nodes = doc.querySelectorAll('script');
    for (var i = 0; i < nodes.length; i++) {
      var old = nodes[i];
      var s = document.createElement('script');
      // ⚠️ 用 getAttribute 拿原始值再自己拼前缀：old.src 在 DOMParser 出来的文档里
      //    会被按 about:blank 解析，拿到的东西不可靠。
      var raw = old.getAttribute('src');
      if (raw) s.src = absUrl(raw, base); else s.textContent = old.textContent;
      host.appendChild(s);
    }
  }

  function loadPanel(id) {
    active = id;                       // 先把"当前面板"定下来，脚本起来时的请求才会走对前缀
    var host = document.getElementById(id);
    if (!host || loaded[id]) return host;
    var src = host.getAttribute('data-src');
    if (!src) return host;
    loaded[id] = true;
    host.setAttribute('data-loading', '1');

    // 取这一页时带个 ?_base=<前缀>：装进主程序页后地址栏会变成 /console/notes，
    // 页面自己拿 location 推出来的 API 前缀就全错了（2026-10-11 抓到「加载失败: bad json」）。
    // 这一页是**反代过来**的，会被重解析，所以它读 /notes/?_base=/notes/ 时 location 是对的。
    var srcUrl = src;
    try {
      var bp = String(src).replace(/[^\/]*$/, '');
      if (bp.charAt(0) !== '/') bp = '/' + bp;
      if (!loaded['__base_' + id]) { srcUrl = src + '?_base=' + encodeURIComponent(bp); }
    } catch (e) {}

    fetch(srcUrl, { cache: 'no-store' })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        // 把"这一页本来是挂在哪个前缀下"告诉它自己：装进主程序页后地址栏会变成
        // /console/notes，页面里拿 location 推出来的 API 前缀就全错了（2026-10-11）。
        // ⚠️ 用 createElement 往真 DOM 里插 head 的第一个孩子 —— 往 HTML 字符串里塞
        //    "<head>…<base>" 这招不灵：碎片解析会把 head 标签连同后一个标签一起吃掉。
        // ⚠️ marker 必须插进**真文档**里的面板容器：插进 DOMParser 出来那个 doc 的 head
        //    是白插 —— head 根本不会被搬进页面（踩了两次，2026-10-11）。
        try {
          var basePath = String(src).replace(/[^\/]*$/, '');
          if (basePath.charAt(0) !== '/') basePath = '/' + basePath;
          var m = document.createElement('script');
          m.type = 'application/x-shuge-panel-base';
          m.setAttribute('data-base', basePath);
          m.textContent = '/* shuge-panel-base ' + basePath + ' */';
          host.appendChild(m);
        } catch (e) {}
        var scopeSel = '#' + id;
        var basePath2 = String(src).replace(/[^\/]*$/, '');
        if (basePath2.charAt(0) !== '/') basePath2 = '/' + basePath2;
        adoptStyles(doc, host, scopeSel, basePath2);
        var kids = doc.body ? doc.body.childNodes : [];
        for (var i = 0; i < kids.length; i++) {
          var node = kids[i];
          if (node.nodeType === 1 && node.tagName.toLowerCase() === 'script') continue;
          host.appendChild(document.importNode(node, true));
        }
        adoptScripts(doc, host, basePath2);
        host.removeAttribute('data-loading');
        host.setAttribute('data-loaded', '1');
        try { window.dispatchEvent(new CustomEvent('shuge:panel-ready', { detail: { id: id } })); } catch (e) {}
      })
      .catch(function (e) {
        loaded[id] = false;
        host.removeAttribute('data-loading');
        host.setAttribute('data-error', String(e));
      });
    return host;
  }

  window.loadPanel = loadPanel;
  window.__shugePanelActive = function () { return active; };
})();
