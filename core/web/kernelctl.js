// 内核页顶部的小控制面板（辞玖 2026-09-20："内核页最顶上放一个启停按钮…下面才是主体"）。
//
// 它同时被两个页面用：
//   · 内核界面（/core/，内核自己发的页）
//   · 内核没在跑时的**降级页**（主进程发的，body 上带 data-kdown="1"）
// 所以这里只用固定的几个 id 操作 DOM（kpState / kpMeta / kpToggle / kpRestart / kpAuto / kpNote），
// 两个页面的那份标记保持一致即可。接口全部走主进程（同源 8790）：启停内核是主进程的职责。
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };

  function fmtUp(s) {
    s = Math.max(0, parseInt(s || 0, 10));
    if (s < 60) return s + ' 秒';
    if (s < 3600) return Math.floor(s / 60) + ' 分 ' + (s % 60) + ' 秒';
    return Math.floor(s / 3600) + ' 小时 ' + Math.floor((s % 3600) / 60) + ' 分';
  }
  function ago(at) {
    if (!at) return '';
    var sec = Math.max(0, Math.round((Date.now() - new Date(at).getTime()) / 1000));
    return sec <= 1 ? '刚刚' : (sec + ' 秒前');
  }
  function note(t) { var el = $('kpNote'); if (el) el.textContent = t; }

  function render(d) {
    window.__kpStatus = d; // 给两个动作函数看（省得重复请求）
    var c = d.core || {}, on = !!c.online;
    var st = $('kpState');
    if (st) { st.textContent = on ? '✅ 在跑' : '⛔ 未运行'; st.className = 'kp-tag ' + (on ? 'ok' : 'bad'); }
    var bits = [];
    if (on) {
      bits.push('pid ' + c.pid);
      bits.push('已运行 ' + fmtUp(c.uptime_s));
      if (c.seq) bits.push('账本 seq ' + c.seq);
    }
    bits.push('自动重连：' + (d.core_autoreconnect ? '开' : '关'));
    if (d.core_checked_at) bits.push('采样：' + ago(d.core_checked_at));
    var m = $('kpMeta');
    if (m) m.textContent = bits.join(' · ');

    var t = $('kpToggle');
    if (t) { t.textContent = on ? '■ 停止内核' : '▶ 启动内核'; t.className = 'kp-btn ' + (on ? 'stop' : 'main'); }
    var a = $('kpAuto');
    if (a) {
      a.textContent = d.core_autoreconnect ? '自动重连：开 ✓' : '自动重连：关 ✗';
      a.className = 'kp-btn' + (d.core_autoreconnect ? '' : ' off');
    }
    // 降级页上，内核一旦起来就换成真正的内核界面（用不着人工刷新）
    if (on && document.body.getAttribute('data-kdown') === '1') {
      note('内核起来了，正在打开内核界面…');
      setTimeout(function () { location.href = '/core/'; }, 1200);
    }
  }

  function tick() {
    fetch('/api/host/status', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () {
        var st = $('kpState');
        if (st) { st.textContent = '读不到主进程'; st.className = 'kp-tag bad'; }
      });
  }

  function act(url, body, done) {
    var opt = { method: 'POST' };
    if (body) { opt.headers = { 'Content-Type': 'application/json' }; opt.body = JSON.stringify(body); }
    fetch(url, opt).then(function (r) { return r.json(); }).then(function (d) {
      if (done) done(d);
      setTimeout(tick, 600);
      setTimeout(tick, 2500);
    }).catch(function (e) { note('请求失败：' + e); });
  }

  // 启停 / 重启
  window.kpAct = function (what) {
    var on = !!(window.__kpStatus && window.__kpStatus.core && window.__kpStatus.core.online);
    var action = (what === 'restart') ? 'restart' : (on ? 'stop' : 'start');
    if (action === 'stop' && !confirm('停止内核？\n\n· 笔记照常读写检索，只是事件/账本/插件/agent 桥停用\n· 停止后不会自动拉起（「自动重连」也会被关掉）')) return;
    if (action === 'restart' && !confirm('重启内核？\n\n· 先优雅停机再拉起（约 3~5 秒）\n· 期间这个页面短暂不可用')) return;
    note('执行中…');
    act('/api/core/' + action, null, function (d) {
      note((d && d.ok === false) ? ('没执行：' + (d.err || '未知原因')) : '已请求：' + action);
    });
  };

  // 自动重连开关
  window.kpToggleAuto = function () {
    var cur = !!(window.__kpStatus && window.__kpStatus.core_autoreconnect);
    var next = !cur;
    if (!next && !confirm('关掉内核的「自动重连」？\n\n· 正在跑的内核不动\n· 它下次挂掉就不会自动拉起了（要回来手动打开这一项）')) return;
    act('/api/core/autoreconnect', { on: next }, function (d) {
      note((d && d.ok === false) ? ('没执行：' + (d.err || ''))
        : (next ? '已打开自动重连，正在确保它在跑…' : '已关掉自动重连（在跑的不动）'));
    });
  };

  tick();
  setInterval(tick, 3000);
})();
