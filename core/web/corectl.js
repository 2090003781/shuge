// 控制台左侧「启停台」面板 + core 在线状态（独立文件，不侵入 app.js）。
//
// 结构（辞玖 2026-09-12 定案）：左边负责启停、右边负责 core 内容。
//   · 左：内嵌「启停台」页面（iframe → 常驻代理 127.0.0.1:<port>/?embed=1）。
//        启停台的 UI 只有一份（在 watchdog 里），这里只是把它嵌进来，不重复实现。
//        core 停了它照样能点「启动」——因为它压根不依赖 core。
//   · 右：core 内容（组件、账本、日志…），core 离线时压暗 + 横幅标注「最后已知状态」。
(function () {
  'use strict';
  const el = (id) => document.getElementById(id);

  // 启停台地址：优先用 core 告诉我们的实际端口（data/serve.port → /api/core/status.control_url），
  // 并记进 localStorage —— core 离线时靠上次记住的地址，左边面板仍然可用。
  let BROKER = (function () {
    try { return localStorage.getItem('sb.control_url') || 'http://127.0.0.1:8791'; }
    catch (e) { return 'http://127.0.0.1:8791'; }
  })().replace(/\/+$/, '');

  let lastOkAt = null;      // 最后确认 core 在线的时间
  let frameBase = null;     // 已加载的启停台地址（避免反复重载 iframe）
  let retryTimer = null;    // 启停台不可用时的重试

  function clock(d) {
    const p = (n) => String(n).padStart(2, '0');
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  // ---- core 离线表现：压暗内容 + 全局横幅 + 卡片置「—」----
  function applyOffline(isOff) {
    document.body.classList.toggle('core-offline', isOff);
    const b = el('coreStaleBanner');
    if (b) {
      b.style.display = isOff ? 'block' : 'none';
      if (isOff) {
        b.textContent = '⚠ core 已离线：右边显示的是最后一次已知状态'
          + (lastOkAt ? '（' + clock(lastOkAt) + '）' : '')
          + '，不是实时数据。启停请到控制台（' + BROKER + '/），它是主进程的一部分，不依赖内核。';
      }
    }
    if (isOff) {
      ['nPlugins', 'nSubs', 'nProcs'].forEach((id) => { const n = el(id); if (n) n.textContent = '—'; });
    }
  }

  // ---- 左侧面板：内嵌启停台 ----
  async function brokerUp() {
    try {
      const r = await fetch(BROKER + '/healthz', { cache: 'no-store' });
      return r.ok;
    } catch (e) { return false; }
  }

  function panelTip(msg) {
    const tip = el('corePanelTip');
    if (tip) { tip.textContent = msg || ''; tip.style.display = msg ? 'block' : 'none'; }
  }

  function mountFrame() {
    const f = el('coreFrame');
    if (!f || frameBase === BROKER) return;
    frameBase = BROKER;
    f.src = BROKER + '/console?embed=1';
    const lk = el('coreFrameOpen');
    if (lk) lk.href = BROKER + '/console';
  }

  async function refreshPanel() {
    const f = el('coreFrame');
    const up = await brokerUp();
    if (up) {
      if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
      mountFrame();
      if (f) f.style.display = '';
      panelTip('');
      return;
    }
    if (f) f.style.display = 'none';
    panelTip('主进程没在运行（' + BROKER + '）—— 双击桌面图标「枢阁」即可恢复；本面板 8 秒后自动重试。');
    if (!retryTimer) {
      retryTimer = setTimeout(() => { retryTimer = null; refreshPanel(); }, 8000);
    }
  }

  // ---- 轮询 ----
  async function load() {
    try {
      const st = await fetchJSON('/api/core/status');
      lastOkAt = new Date();
      if (st.control_url) {
        const u = String(st.control_url).replace(/\/+$/, '');
        if (u !== BROKER) { BROKER = u; frameBase = null; } // 端口变了 → 换地址重挂
        try { localStorage.setItem('sb.control_url', BROKER); } catch (e) { /* 隐私模式忽略 */ }
      }
      applyOffline(false);
    } catch (e) {
      applyOffline(true);
    }
    refreshPanel();
  }

  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => { load(); setInterval(load, 5000); });
  } else {
    load();
    setInterval(load, 5000);
  }
})();
