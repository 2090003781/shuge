// 前端冒烟：用 Node 模拟浏览器加载 web/index.html 的内联脚本，
// 打桩 fetch 后验证：初始化会拉树与统计、搜索会渲染命中、保存会 PUT。
// 用法：node tools/smoke_ui.cjs   （服务需已在 8848 运行，或纯桩也可）
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const htmlPath = path.join(__dirname, '..', 'web', 'index.html');
const html = fs.readFileSync(htmlPath, 'utf8');

// 抽出内联脚本（最后一个 <script> 块）
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.log('❌ 没找到内联脚本'); process.exit(1); }
const code = m[1];

// ── 最小 DOM 打桩 ─────────────────────────────────────────
const store = {};
function mkEl(id) {
  return store[id] = store[id] || {
    id, innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
    children: [], classList: { _s: new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);}, contains(c){return this._s.has(c);}, toggle(){} },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {}, setAttribute() {}, getAttribute: () => null,
    firstElementChild: { textContent: '' }, querySelectorAll: () => [],
  };
}
const calls = [];
let treeResp = { ok: true, tree: { id: 'r', name: '', type: 'dir', children: [
  { id: 'd1', name: '产品化', type: 'dir', children: [], files: [{ id: 'f1', name: 'FORMAT-数据格式规范-v0.4.md', kind: 'markdown', visibility: 'internal' }] },
], files: [] } };
let healthResp = { ok: true, stats: { files: 12, chunks: 198, disk_bytes: 2638200 } };
let searchResp = { ok: true, mode: 'hybrid', hits: [
  { name: 'FORMAT-数据格式规范-v0.4.md', heading: '4.2 私密加锁', text: '默认锁定 + 密码查看', channels: ['fts','vec'], rrf: 0.032, vec_score: 0.71 },
] };
let fileResp = { ok: true, file: { id: 'f1', name: 'FORMAT-数据格式规范-v0.4.md', kind: 'markdown', size: 100, mtime: '2026-09-13T22:00:00+08:00', content: '# 标题\n正文' } };

const sandbox = {
  location: { pathname: '/' },                       // 页面用它算 API 前缀
  document: {
    getElementById: (id) => mkEl(id),
    querySelectorAll: () => [],
    createElement: () => mkEl('tmp' + Object.keys(store).length),
    addEventListener() {},
  },
  window: { addEventListener() {} },
  fetch: async (url, opts) => {
    const method = (opts && opts.method) || 'GET';
    calls.push(method + ' ' + url);
    const pick = (obj) => ({ ok: true, status: 200, json: async () => obj });
    if (url.startsWith('/api/tree')) return pick(treeResp);
    if (url.startsWith('/api/health')) return pick(healthResp);
    if (url.startsWith('/api/search')) return pick(searchResp);
    if (url.startsWith('/api/files/')) return pick(method === 'PUT' ? { ok: true, saved: { chunks: 7 } } : fileResp);
    if (url.startsWith('/api/files')) return pick({ ok: true, file_id: 'new1' });
    return pick({ ok: false, error: 'unexpected ' + url });
  },
  setTimeout: (f) => { try { f(); } catch (e) {} return 0; },
  clearTimeout: () => {},
  alert: (s) => calls.push('alert ' + s),
  confirm: () => true,
  prompt: (q, d) => d || '新笔记.md',
  console,
  encodeURIComponent, decodeURIComponent, Error, JSON, String, Number, Promise, Object, Array, Set,
};

let pass = 0, fail = 0;
function check(name, cond, extra) {
  cond ? pass++ : fail++;
  console.log((cond ? '  ✅ ' : '  ❌ ') + name + (extra === undefined ? '' : ' | ' + String(extra).slice(0, 90)));
}
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

(async () => {
  try { vm.runInContext(code, sandbox, { filename: 'inline.js' }); console.log('① 内联脚本执行: OK'); }
  catch (e) { console.log('① 执行抛错:', e.message); process.exit(1); }

  await new Promise(r => setTimeout(r, 30));
  check('初始化拉了树与统计',
    calls.some(c => c.includes('/api/tree')) && calls.some(c => c.includes('/api/health')));
  check('统计条渲染出文件数', (store['stats'] || {}).textContent && store['stats'].textContent.includes('文件'),
    (store['stats'] || {}).textContent);

  check('树渲染出内容', store['tree'] && store['tree'].children.length > 0,
    store['tree'] ? store['tree'].children.length : 0);

  console.log('② 文件夹行标签（回归：点一下不能把 emoji 拆成半个代理对）');
  const LONE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;
  check('折叠时 📁', sandbox.dirLabel('产品化', false) === '📁 产品化', sandbox.dirLabel('产品化', false));
  check('展开时 📂', sandbox.dirLabel('产品化', true) === '📂 产品化', sandbox.dirLabel('产品化', true));
  check('根节点保持 🏠', sandbox.dirLabel('', true) === '🏠 根目录', sandbox.dirLabel('', true));
  const fhead = { dataset: { dirName: '产品化' }, firstElementChild: { textContent: '📁 产品化' } };
  const fbody = { style: { display: 'none' } };
  sandbox.toggle(fhead, fbody);
  check('点一下 → 展开', fhead.firstElementChild.textContent === '📂 产品化', fhead.firstElementChild.textContent);
  check('没有孤立代理对（不是乱码）', !LONE.test(fhead.firstElementChild.textContent),
    JSON.stringify(fhead.firstElementChild.textContent));
  sandbox.toggle(fhead, fbody);
  check('再点一下 → 折叠回 📁', fhead.firstElementChild.textContent === '📁 产品化',
    fhead.firstElementChild.textContent);
  check('来回切也不残留', !LONE.test(fhead.firstElementChild.textContent) && fbody.style.display === 'none',
    JSON.stringify(fhead.firstElementChild.textContent) + ' display=' + JSON.stringify(fbody.style.display));
  const rhead = { dataset: { dirName: '' }, firstElementChild: { textContent: '🏠 根目录' } };
  const rbody = { style: { display: '' } };
  sandbox.toggle(rhead, rbody);
  check('根节点切换后仍是 🏠 根目录', rhead.firstElementChild.textContent === '🏠 根目录',
    rhead.firstElementChild.textContent);

  await sandbox.openFile('f1', mkEl('row1'));
  check('打开文件后编辑器有正文', (store['editor'].value || '').includes('标题'), store['editor'].value);
  check('meta 条显示文件名', (store['meta'].innerHTML || '').includes('FORMAT-数据格式规范'));

  store['q'].value = '私密文件怎么加锁';
  await sandbox.doSearch();
  check('搜索命中渲染到面板', store['hits'].innerHTML.includes('私密加锁'), store['hits'].innerHTML.slice(0, 80));
  check('搜索走的是 hybrid 接口', calls.some(c => c.includes('/api/search')));

  store['editor'].value = '# 改过的内容';
  await sandbox.save();
  check('保存发出 PUT', calls.some(c => c.startsWith('PUT')));

  await sandbox.newFile();
  check('新建发出 POST', calls.some(c => c.startsWith('POST')));

  console.log('\n========  结论: %d 通过 / %d 失败', pass, fail);
  process.exit(fail ? 1 : 0);
})();
