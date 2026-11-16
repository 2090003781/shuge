// 验证"点击搜索条目 → 右侧抽屉显示内容（不动编辑器）"
// 用法：node tools/verify_hit_locate.cjs [hits.json]
//   不给参数时**当场搜**（默认词「引用」）—— 不再用抓取下来的快照当夹具：
//   那种文件里带着真 file_id / 真文件名 / 真正文片段，跟着程序包一起发出去就是数据遗留
//   （2026-10-06 辞玖「再检查一下，程序、插件等等，没有任何数据遗留」时抓到的唯一一处）。
// 1) openHit 只开抽屉：编辑器内容必须一字不变，抽屉里必须有 <mark> 命中段
// 2) jumpToHit（抽屉底部按钮）才动编辑器：打开文件 + 选中命中位置 + 滚动
const fs = require('node:fs');
const vm = require('node:vm');
const http = require('node:http');

function get(p) {
  return new Promise((res, rej) => {
    http.get({ host: '127.0.0.1', port: 8790, path: p }, (r) => {
      let b = ''; r.setEncoding('utf8'); r.on('data', c => b += c); r.on('end', () => res(b));
    }).on('error', rej);
  });
}

function mkEl(tag) {
  const cls = new Set();
  return {
    tagName: tag, innerHTML: '', textContent: '', value: '', style: {}, dataset: {}, children: [], attrs: {},
    className: '', scrollTop: 0, selection: null, focused: false,
    classList: { add: c => cls.add(c), remove: c => cls.delete(c), contains: c => cls.has(c), toggle: c => cls.has(c) ? cls.delete(c) : cls.add(c) },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {}, setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k] ?? null; },
    querySelectorAll: () => [], focus() { this.focused = true; },
    setSelectionRange(a, b) { this.selection = [a, b]; },
    get firstElementChild() { return this.children[0] || { textContent: '' }; },
  };
}

const strip = (s) => String(s).replace(/<[^>]*>/g, '').replace(/\s+/g, '');

// 当场搜出命中（形状与 /api/search 返回一致；只留"读得到、片段够长"的）
// 读得到 = /api/files/<id>/text 不报错（私密未解锁/已删的会报错 → 跳过，不然抽屉必然空）
async function liveHits(q) {
  // 注意前缀：页面是 /notes/ 反代过来的，命中接口在 /notes/api/search（直接写 /api/search 是 404）
  const r = JSON.parse(await get('/notes/api/search?limit=20&q=' + encodeURIComponent(q)));
  const all = (r.hits || []).filter((h) => h && h.file_id && h.name && (h.text || '').length >= 24);
  const usable = [];
  for (const h of all) {
    if (usable.length >= 6) break;
    try {
      const t = await get('/notes/api/files/' + h.file_id + '/text');
      JSON.parse(t);                                     // 报错就不是合法 JSON → 读不到
      usable.push(h);
    } catch (e) { /* 读不到（私密/已删）→ 跳过 */ }
  }
  return usable;
}

(async () => {
  const html = await get('/notes/');
  const byId = {};
  const hitsFile = process.argv[2];
  const q = hitsFile ? '私密加锁' : (process.env.HIT_Q || '引用');
  let hits;
  if (hitsFile) {
    if (!fs.existsSync(hitsFile)) { console.log('找不到命中文件: ' + hitsFile); process.exit(1); }
    hits = JSON.parse(fs.readFileSync(hitsFile, 'utf8'));
  } else {
    hits = await liveHits(q);
    if (hits.length < 4) { console.log('当场搜索没凑够 4 条可读命中（q=%s，得到 %d 条）', q, hits.length); process.exit(1); }
  }
  const sandbox = {
    location: { pathname: '/notes/' },
    document: { getElementById: (id) => (byId[id] = byId[id] || Object.assign(mkEl('div'), { id })),
                createElement: (t) => mkEl(t), querySelectorAll: () => [], addEventListener() {} },
    getComputedStyle: () => ({ lineHeight: '22px' }),
    fetch: async (url) => {
      const p = String(url).replace(/^https?:\/\/[^/]+/, '');
      const body = await get(p);
      return { ok: true, status: 200, json: async () => JSON.parse(body) };
    },
    setTimeout, clearTimeout, setImmediate,
    alert: () => {}, confirm: () => true, prompt: () => null,
    console: { log(){}, error(){} },
    encodeURIComponent, decodeURIComponent, Error, JSON, String, Number, Promise, Object, Array, RegExp, Math, parseFloat, Set,
  };
  sandbox.addEventListener = () => {};
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], sandbox, { filename: 'inline.js' });
  for (let w = 0; w < 500 && !(byId['stats'] && byId['stats'].textContent); w++) await new Promise(r => setTimeout(r, 10));
  console.log('① 初始化 OK | stats:', byId['stats'].textContent);

  // 惰性取元素：页面脚本按需 getElementById，这里必须同样按需创建
  const el = (id) => (byId[id] = byId[id] || Object.assign(mkEl('div'), { id }));
  const ta = () => el('editor'), drawer = () => el('drawer'), dBody = () => el('dBody'), dTitle = () => el('dTitle');
  sandbox.__q = q;
  sandbox.__hits = hits;
  console.log('② 模拟搜索：注入 %d 条命中（q=%s）', hits.length, q);

  // 先打开一个无关文件，制造"编辑器里已有内容"的现场
  await sandbox.openFile(hits[hits.length - 1].file_id, null);
  await new Promise(r => setImmediate(r));
  const before = ta().value;

  let pass = 0, fail = 0;
  const n = Math.min(hits.length, 4);
  for (let i = 0; i < n; i++) {
    const h = hits[i];
    await sandbox.openHit(i);
    await new Promise(r => setImmediate(r));
    const body = String(dBody().innerHTML);
    const hasMark = body.includes('<mark>');
    const sameEditor = ta().value === before;
    const opened = drawer().classList.contains('open');
    const probe = (h.text || '').replace(/\s+/g, '');
    const inDrawer = body.replace(/\s+/g, '').includes(probe.slice(0, 20));
    const ok = opened && hasMark && sameEditor && inDrawer;
    ok ? pass++ : fail++;
    console.log('   [%d] %s → 抽屉 %s | 命中段<mark> %s | 编辑器不变 %s | 内容对上 %s | dBody %d 字符 | 匹配 %s',
      i, h.name.slice(0, 24), opened ? '打开' : '未开', hasMark ? '有' : '无',
      sameEditor ? '是' : '否', inDrawer ? '是' : '否', body.length, ok ? '✅' : '❌');
    if (!ok) console.log('        标题:', dTitle().textContent, '| dBody 开头:', JSON.stringify(body.slice(0, 120)));
  }

  // 抽屉底部按钮：这条路才动编辑器
  const last = Math.min(hits.length, 4) - 1;
  const h = hits[last];
  await sandbox.jumpToHit();
  await new Promise(r => setImmediate(r));
  const sel = ta().selection ? ta().value.slice(ta().selection[0], ta().selection[1]) : '';
  const probe = (h.text || '').replace(/\s+/g, '').slice(0, 24);
  const jumpOk = !!sel && sel.replace(/\s+/g, '').startsWith(probe.slice(0, 10)) && ta().scrollTop > 0;
  jumpOk ? pass++ : fail++;
  console.log('③ jumpToHit（抽屉按钮）→ 编辑器 %d 字符 | 选区 %s | scrollTop=%d | 匹配 %s',
    ta().value.length, ta().selection ? `[${ta().selection[0]},${ta().selection[1]}]` : '无', ta().scrollTop, jumpOk ? '✅' : '❌');

  // 关闭
  sandbox.closePeek();
  const closed = !drawer().classList.contains('open');
  closed ? pass++ : fail++;
  console.log('④ closePeek → 抽屉 %s | 匹配 %s', closed ? '已收起' : '仍打开', closed ? '✅' : '❌');

  console.log('⑤ 结论: %d 通过 / %d 失败', pass, fail);
  process.exit(fail ? 1 : 0);
})();
