// 统一日志视图（2026-09-12）——消费平台输出中枢的只读接口：
//   GET /api/logs/sources                有哪些来源、各多大、最后写入时间
//   GET /api/logs?source=<x>&tail=N      某来源最近 N 行（统一格式行）
//
// 设计要点：
//   · 平台不做"控制台宿主"，而做"输出中枢"：所有子进程输出统一格式落在 data/logs/<source>.log
//   · 本视图只读、只展示，"聚合"模式把多来源按时间戳合并成一条时间线（这才是"统一视图"）
//   · 独立成文件：不侵入 app.js，只借用它的 VIEWS/NAVS/go/fetchJSON/esc
(function () {
  'use strict';

  // ---- 挂进既有视图路由 ----
  VIEWS.logs = 'view-logs';
  NAVS.logs = 'nav-logs';
  const _go = window.go;
  window.go = function (name) {
    _go(name);
    if (name === 'logs') { tickLogs(); startAuto(); } else { stopAuto(); }
  };

  const state = { source: '__all__', tail: 300, kw: '', level: '', auto: true, timer: null, sources: [], lastAt: '' };

  const LEVEL_COLOR = { INFO: 'var(--cyan)', WARN: 'var(--amber)', ERROR: 'var(--red)' };

  // 统一格式行： 2026-09-12T11:30:04+08:00 | INFO  | plugin.context-pack | 正文
  const LINE_RE = /^(\S+)\s*\|\s*(\S+)\s*\|\s*([^|]*?)\s*\|\s*([\s\S]*)$/;

  function parseLine(line) {
    const m = line.match(LINE_RE);
    if (!m) return { ts: '', level: '', source: '', text: line };
    return { ts: m[1], level: m[2].trim(), source: m[3].trim(), text: m[4] };
  }

  function fmtSize(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }

  function hhmmss(ts) {
    const m = String(ts || '').match(/T(\d{2}:\d{2}:\d{2})/);
    return m ? m[1] : (ts || '');
  }

  // ---- 数据 ----
  window.tickLogs = async function () {
    try {
      await loadSources();
      await loadStream();
    } catch (e) { /* 静默：日志视图不该打扰主流程 */ }
  };

  async function loadSources() {
    const list = await fetchJSON('/api/logs/sources');
    state.sources = list || [];
    renderSources();
  }

  async function loadStream() {
    const box = document.getElementById('logStream');
    if (!box) return;
    let lines = [];
    if (state.source === '__events__') {
      // 平台事件（总线）：取自账本查询接口——字段更全（含 payload），且是持久真相源
      const rows = await fetchJSON('/api/events/search?level=all&limit=' + state.tail).catch(() => []);
      lines = (rows || []).map(r => {
        let p = '';
        try { p = r.payload ? JSON.stringify(r.payload) : ''; } catch (e) { p = ''; }
        if (p.length > 400) p = p.slice(0, 400) + '…';
        return [r.ts || '', r.type || '', r.source || '', p].join(' | ');
      });
    } else if (state.source === '__all__') {
      if (!state.sources.length) { lines = []; }
      else {
        const per = Math.max(30, Math.floor(state.tail / state.sources.length));
        const chunks = await Promise.all(state.sources.map(s =>
          fetchJSON('/api/logs?source=' + encodeURIComponent(s.source) + '&tail=' + per)
            .then(r => r.lines || []).catch(() => [])));
        lines = [].concat.apply([], chunks);
        // 按时间戳合并成一条时间线（新→旧）；同戳保持稳定
        lines.sort((a, b) => {
          const ta = (a.match(LINE_RE) || [])[1] || '';
          const tb = (b.match(LINE_RE) || [])[1] || '';
          return tb.localeCompare(ta);
        });
        if (lines.length > state.tail) lines = lines.slice(0, state.tail);
      }
    } else {
      const r = await fetchJSON('/api/logs?source=' + encodeURIComponent(state.source) + '&tail=' + state.tail);
      lines = r.lines || [];
    }
    state.lastAt = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    renderStream(lines);
  }

  // ---- 渲染 ----
  function renderSources() {
    const box = document.getElementById('logSources');
    if (!box) return;
    const all = state.sources.reduce((n, s) => n + (s.bytes || 0), 0);
    const rows = [];
    rows.push(sourceRow('__all__', '全部（聚合时间线）', all, state.sources.length ? state.sources[0].updated : ''));
    rows.push(sourceRow('__events__', '⚡ 平台事件（总线·账本）', 0, ''));
    for (const s of state.sources) rows.push(sourceRow(s.source, s.source, s.bytes, s.updated));
    box.innerHTML = rows.join('');
  }

  function sourceRow(id, label, bytes, updated) {
    const active = state.source === id ? ' active' : '';
    return '<div class="log-src' + active + '" onclick="selectLogSource(\'' + esc(id) + '\')">'
      + '<div class="nm">' + esc(label) + '</div>'
      + '<div class="meta">' + fmtSize(bytes || 0) + (updated ? ' · ' + hhmmss(updated) : '') + '</div>'
      + '</div>';
  }

  window.selectLogSource = function (id) {
    state.source = id;
    renderSources();
    loadStream().catch(() => {});
  };

  window.setLogTail = function (v) { state.tail = parseInt(v, 10) || 300; loadStream().catch(() => {}); };
  window.setLogLevel = function (v) { state.level = v; loadStream().catch(() => {}); };
  window.setLogKw = function (v) { state.kw = (v || '').trim(); loadStream().catch(() => {}); };
  window.toggleLogAuto = function (on) { state.auto = !!on; if (on) startAuto(); else stopAuto(); };

  function renderStream(lines) {
    const box = document.getElementById('logStream');
    const info = document.getElementById('logInfo');
    if (!box) return;
    const kw = state.kw.toLowerCase();
    const lv = state.level;
    const shown = [];
    for (const line of lines) {
      const p = parseLine(line);
      if (lv && p.level !== lv) continue;
      if (kw && line.toLowerCase().indexOf(kw) < 0) continue;
      shown.push(p);
      if (shown.length >= state.tail) break;
    }
    if (!shown.length) {
      box.innerHTML = '<div class="log-empty">没有匹配的日志（试试放宽级别/关键字，或换一个来源）</div>';
    } else {
      box.innerHTML = shown.map(p => {
        const l = p.level || '—';
        const color = LEVEL_COLOR[l] || 'var(--dim)';
        let text = esc(p.text);
        if (kw) {
          try {
            text = text.replace(new RegExp('(' + kw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi'), '<mark>$1</mark>');
          } catch (e) { /* 忽略非法正则 */ }
        }
        return '<div class="log-line">'
          + '<span class="t">' + esc(hhmmss(p.ts)) + '</span>'
          + '<span class="lv" style="color:' + color + '">' + esc(l) + '</span>'
          + '<span class="src">' + esc(p.source) + '</span>'
          + '<span class="txt">' + text + '</span>'
          + '</div>';
      }).join('');
    }
    if (info) {
      info.textContent = '显示 ' + shown.length + ' / ' + lines.length + ' 行 · 最后刷新 ' + state.lastAt
        + (state.auto ? ' · 自动刷新 3s' : ' · 已暂停');
    }
  }

  // ---- 自动刷新（只在日志视图打开且勾选时跑）----
  function startAuto() {
    stopAuto();
    if (!state.auto) return;
    state.timer = setInterval(() => {
      if ((location.hash.replace('#/', '') || 'notes') !== 'logs') { stopAuto(); return; }
      tickLogs();
    }, 3000);
  }
  function stopAuto() {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
  }
})();
