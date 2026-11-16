// 主题统一自测（Node + vm + DOM 打桩）：直接加载**真源码** scripts/theme_boot.js，验四件事：
//   ① 首次加载：用本页已知值（localStorage/父页面/默认）应用，**不往服务端写**（否则新页面会把别人的设置冲掉）
//   ② 用户点击切换：写 localStorage + **POST /api/ui/theme** + **把主题推进每一个同页 iframe**（← 辞玖截图那个 bug）
//   ③ 收到 message（父页面/别的页面推来的）：只应用，**不回推**（防循环）
//   ④ 被嵌在 iframe 里时：跟随父页面的主题，并隐藏自己的主题按钮（免得跟外层重复）
//
// 跑法：node web/_theme_selftest.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const CODE = fs.readFileSync(path.join(__dirname, '..', 'scripts', 'theme_boot.js'), 'utf8');
const fails = [];
function check(name, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '  ← ' + extra));
  if (!cond) fails.push(name);
}

function mkDoc(theme) {
  const attrs = theme ? { 'data-theme': theme } : {};
  return {
    attrs,
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
  };
}

function load(opts) {
  opts = opts || {};
  const state = { fetches: [], posts: [], msgs: [], handlers: {}, bc: [], bcSent: [], streams: [] };
  const btn = { id: 'btnTheme', textContent: '', style: {} };
  const iframes = (opts.iframes || 2);
  const frameDocs = [];
  const frames = [];
  for (let i = 0; i < iframes; i++) {
    const fd = { documentElement: mkDoc(null) };        // ⚠️ 跟真 DOM 一样：document.documentElement
    frameDocs.push(fd);
    frames.push({
      contentDocument: fd,
      contentWindow: { postMessage: (m) => state.posts.push(m), __shugeTheme: null },
    });
  }
  const doc = {
    readyState: 'complete',
    documentElement: mkDoc(null),
    getElementById: (id) => (id === 'btnTheme' ? btn : null),
    querySelectorAll: (sel) => (sel === 'iframe' ? frames : []),
    addEventListener: (ev, fn) => { state.handlers[ev] = fn; },
  };
  const win = {
    parent: opts.parent || null,
    top: null,
    self: null,
    addEventListener: (ev, fn) => { state.handlers[ev] = fn; },
  };
  win.self = opts.embedded ? { fake: 'child' } : win;
  win.top = opts.embedded ? { fake: 'top' } : win;
  if (!win.parent) win.parent = win;

  const store = Object.assign({}, opts.store || {});
  const ctx = {
    document: doc,
    window: win,
    localStorage: {
      getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
    },
    location: { search: opts.search || '' },
    URLSearchParams: class { constructor(s) { this.s = s || ''; } get(k) { const m = new RegExp('[?&]' + k + '=([^&]*)').exec(this.s); return m ? m[1] : null; } },
    fetch: (url, opt) => {
      state.fetches.push({ url, opt });
      return Promise.resolve({ json: () => Promise.resolve({ theme: opts.apiTheme || 'dark' }) });
    },
    console, Promise, JSON, Object, String, RegExp, setTimeout, setInterval, clearInterval, Error,
    // 同一个源的两个标签页之间：BroadcastChannel（真浏览器里跨端口收不到，但同源能收到）
    BroadcastChannel: class {
      constructor(name) { this.name = name; state.bc.push(this); }
      postMessage(m) { state.bcSent.push(m); state.msgs.push(m); }
      addEventListener(ev, fn) { if (ev === 'message') this.onmessage = fn; }
      close() {}
    },
    // SSE：页面连 /api/ui/theme/stream 收实时主题（辞玖 2026-10-06「全实时」）
    EventSource: opts.noSSE ? undefined : class {
      constructor(url) { this.url = url; this.handlers = {}; state.streams.push(this); }
      addEventListener(ev, fn) { this.handlers[ev] = fn; }
      close() {}
      emit(ev, data) { if (this.handlers[ev]) this.handlers[ev]({ data: JSON.stringify(data) }); }
    },
  };
  ctx.window.__shugeTheme = null;
  vm.createContext(ctx);
  vm.runInContext(CODE, ctx);
  return { ctx, state, doc, btn, frameDocs, frames, store, win };
}

// ① 首次加载：默认深色、不写服务端、按钮文案是"切到浅色"
{
  const t = load({});
  check('① 首次加载默认深色', t.doc.documentElement.getAttribute('data-theme') === 'dark',
        t.doc.documentElement.getAttribute('data-theme'));
  check('① 首次加载**不往服务端写**（只 GET 拉一次真相）',
        t.state.fetches.length === 1 && (!t.state.fetches[0].opt || t.state.fetches[0].opt.method !== 'POST'),
        JSON.stringify(t.state.fetches.map((f) => f.opt && f.opt.method)));
  check('① 按钮文案 = 切到浅色', t.btn.textContent.indexOf('浅白') >= 0, t.btn.textContent);
}

// ② 点击切换：写 localStorage + POST + 推进每个 iframe
{
  const t = load({ store: { 'shuge-theme': 'dark' } });
  t.ctx.window.__shugeTheme.toggle();
  check('② 切换后 data-theme=light', t.doc.documentElement.getAttribute('data-theme') === 'light',
        t.doc.documentElement.getAttribute('data-theme'));
  const posts = t.state.fetches.filter((f) => f.opt && f.opt.method === 'POST');
  check('② 往服务端 POST 了一次（跨端口唯一真相）', posts.length === 1,
        JSON.stringify(t.state.fetches.map((f) => f.opt && f.opt.method)));
  check('② POST 的 body 是 light', posts.length === 1 && /"theme"\s*:\s*"light"/.test(posts[0].opt.body),
        posts.length ? posts[0].opt.body : '(无)');
  check('② 全部 iframe 的 <html> 都跟着变浅（← 截图里"只有页签变了"就是这个）',
        t.frameDocs.every((d) => d.documentElement.getAttribute('data-theme') === 'light'),
        t.frameDocs.map((d) => d.documentElement.getAttribute('data-theme')).join(','));
  check('② 还给每个 iframe 发了 postMessage（跨端口那半截）',
        t.state.posts.filter((m) => m.shugeTheme === 'light').length === t.frameDocs.length,
        JSON.stringify(t.state.posts));
}

// ③ 收到 message：只应用不回推
{
  const t = load({});
  const before = t.state.fetches.length;
  t.state.handlers.message({ data: { shugeTheme: 'light' } });
  check('③ 收到 message 会应用', t.doc.documentElement.getAttribute('data-theme') === 'light',
        t.doc.documentElement.getAttribute('data-theme'));
  check('③ 收到 message **不回推**（没有新的 POST，防循环）',
        t.state.fetches.length === before, t.state.fetches.length + ' vs ' + before);
}

// ④ 被嵌：跟随父页面 + 隐藏自己的按钮
{
  const parent = {
    document: { documentElement: mkDoc('light') },
    postMessage: () => {},
  };
  const t = load({ embedded: true, parent, store: {} });
  check('④ 被嵌时跟随父页面的主题（父=light）',
        t.doc.documentElement.getAttribute('data-theme') === 'light',
        t.doc.documentElement.getAttribute('data-theme'));
  check('④ 被嵌时隐藏自己的主题按钮（外层已经有一个）', t.btn.style.display === 'none', t.btn.style.display);
}

// ⑤ ?theme=light 认（分享链接用）
{
  const t = load({ search: '?theme=light' });
  check('⑤ ?theme=light 直接生效', t.doc.documentElement.getAttribute('data-theme') === 'light',
        t.doc.documentElement.getAttribute('data-theme'));
}

// ⑥ 两个页面互推**不能无限循环**（2026-10-06 振荡事故的回归用例）
{
  // 接线：A / B 互为对方的父窗口 + 子 iframe，postMessage 进队列（默认不走队列，别用 load 造的桩帧）
  const q = [];
  const wire = (from, to) => {
    const toSend = (m) => { q.push([to, m]); };
    from.win.parent = { postMessage: toSend, document: null };
    const f = {
      contentDocument: to.doc,
      contentWindow: { postMessage: toSend, __shugeTheme: to.ctx.window.__shugeTheme },
    };
    from.doc.querySelectorAll = (sel) => (sel === 'iframe' ? [f] : []);   // 以后 from 的"子 iframe"就是 to
  };
  // 泵：把队列抽干（真投递），返回抽了多少段
  function pump() {
    let hops = 0;
    while (q.length && hops < 60) { const [pg, m] = q.shift(); pg.state.handlers.message({ data: m }); hops++; }
    return { hops, stuck: q.length > 0 };
  }
  const A = load({ iframes: 1 });
  const B = load({ iframes: 1 });
  wire(A, B); wire(B, A);                                    // ⚠️ 必须先接好线，再点（上一版就是错在这）
  A.state.fetches.length = 0; B.state.fetches.length = 0;    // 只看消息，不看网络
  A.ctx.window.__shugeTheme.toggle();                        // A 上用户点了一下
  const r = pump();
  check('⑥ 一次点击的消息会停下来（不是无限互推）', !r.stuck && r.hops <= 12, r.hops + ' 段消息，剩 ' + (r.stuck ? '还在转' : '0'));
  const ta = A.doc.documentElement.getAttribute('data-theme');
  const tb = B.doc.documentElement.getAttribute('data-theme');
  check('⑥ 两个页面收敛到同一个主题', ta === tb, ta + ' vs ' + tb);

  // 收到**同值**的推送：直接短路，不再往外发
  q.length = 0;
  const C = load({ iframes: 1 });
  const D = load({ iframes: 1 });
  wire(C, D); wire(D, C);
  pump();
  const before = D.state.fetches.length;
  D.state.handlers.message({ data: { shugeTheme: D.doc.documentElement.getAttribute('data-theme') } });
  const r2 = pump();
  check('⑥ 同值推送被短路（不再往外发一段）', r2.hops === 0 && !r2.stuck, r2.hops + ' 段');
  check('⑥ 同值推送也不写服务端', D.state.fetches.length === before,
        D.state.fetches.length + ' vs ' + before);
}

// ⑦ 同一个源的两个标签（BroadcastChannel）：一个点，另一个跟着变，但**不许推回去**
{
  const A = load({ iframes: 0 });
  const B = load({ iframes: 0 });
  // B 收到 A 的广播（真投递：把 A 发出去的消息喂给 B 的监听）
  const feed = () => { A.state.bcSent.forEach((m) => B.state.bc[0].onmessage({ data: m })); };
  const sentBefore = A.state.bcSent.length;
  A.ctx.window.__shugeTheme.toggle();
  check('⑦ 点了之后往 BroadcastChannel 发了一条', A.state.bcSent.length === sentBefore + 1,
        JSON.stringify(A.state.bcSent));
  const t = A.state.bcSent[A.state.bcSent.length - 1];
  check('⑦ 广播里带着新主题', t && t.shugeTheme === 'light', JSON.stringify(t));
  feed();
  check('⑦ 另一个标签跟着变成同一个主题',
        B.doc.documentElement.getAttribute('data-theme') === 'light',
        B.doc.documentElement.getAttribute('data-theme'));
  check('⑦ 收到广播的一方**不再往服务端写**（防两页互推）',
        B.state.fetches.filter((f) => f.opt && f.opt.method === 'POST').length === 0,
        JSON.stringify(B.state.fetches.map((f) => f.opt && f.opt.method)));
  check('⑦ 收到广播的一方**不再往外广播**（链路到此为止）', B.state.bcSent.length === 0,
        JSON.stringify(B.state.bcSent));
  // 同一个值再广播一次：短路，什么都不做（只看这一下有没有新增的请求/广播，别把 boot 的 GET 算进来）
  const nf = B.state.fetches.length;
  B.state.bc[0].onmessage({ data: { shugeTheme: 'light' } });
  check('⑦ 同值广播被短路（没有新请求、没有新广播）',
        B.state.bcSent.length === 0 && B.state.fetches.length === nf,
        '广播 ' + B.state.bcSent.length + ' 条，请求 ' + (B.state.fetches.length - nf) + ' 个');
}

// ⑧ 实时：连上 SSE，别人一改这边立刻变；但**收到推送的一方不再往外推**（防互推）
{
  const t = load({ iframes: 0 });
  check('⑧ 连上了实时通道（/api/ui/theme/stream）',
        t.state.streams.length === 1 && /\/api\/ui\/theme\/stream$/.test(t.state.streams[0].url),
        JSON.stringify(t.state.streams.map((x) => x.url)));
  const st = t.state.streams[0];
  const posts = () => t.state.fetches.filter((f) => f.opt && f.opt.method === 'POST').length;
  const n0 = posts();
  st.emit('theme', { theme: 'light' });
  check('⑧ 流推来 light → 本页跟着变', t.doc.documentElement.getAttribute('data-theme') === 'light',
        t.doc.documentElement.getAttribute('data-theme'));
  check('⑧ 收推送的一方不写服务端（防互推）', posts() === n0, posts() + ' vs ' + n0);
  check('⑧ 收推送的一方不再广播', t.state.bcSent.length === 0, JSON.stringify(t.state.bcSent));
  st.emit('theme', { theme: 'light' });                       // 同值：短路
  check('⑧ 同值推送被短路（不写服务端）', posts() === n0, posts() + ' vs ' + n0);
  st.emit('theme', { theme: 'dark' });                        // 反向也要通
  check('⑧ 流推来 dark → 本页跟着变', t.doc.documentElement.getAttribute('data-theme') === 'dark',
        t.doc.documentElement.getAttribute('data-theme'));
  check('⑧ 按钮文案跟着换（两个 span 各自更新）', t.btn.textContent.indexOf('浅白') >= 0, t.btn.textContent);
}

// ⑧-b 浏览器不支持 EventSource 时：退回轮询（不是什么都不做）
{
  const t = load({ iframes: 0, noSSE: true });
  check('⑧-b 没有 EventSource 时不报错、页面照常', t.doc.documentElement.getAttribute('data-theme') === 'dark',
        t.doc.documentElement.getAttribute('data-theme'));
  check('⑧-b 装的是轮询兜底（visibilitychange 在）', typeof t.state.handlers.visibilitychange === 'function',
        Object.keys(t.state.handlers).join(','));
}

console.log(fails.length ? '\n失败 ' + fails.length + ' 条：' + fails.join('、') : '\n全部通过');
process.exit(fails.length ? 1 : 0);
