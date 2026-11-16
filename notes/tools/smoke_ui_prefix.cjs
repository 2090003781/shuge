// 验证 ai-notes 前端的"相对前缀"改造：不同挂载路径下，API 请求应拼到正确前缀。
// 用法：node tools/smoke_ui_prefix.cjs
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.log('❌ 没找到内联脚本'); process.exit(1); }
const code = m[1];

function runAt(pathname) {
  const store = {};
  const mkEl = (id) => (store[id] = store[id] || {
    id, innerHTML: '', textContent: '', value: '', style: {}, dataset: {}, children: [],
    classList: { add(){}, remove(){}, contains: () => false, toggle(){} },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {}, setAttribute() {}, getAttribute: () => null,
    firstElementChild: { textContent: '' }, querySelectorAll: () => [],
  });
  const calls = [];
  const sandbox = {
    location: { pathname },
    document: { getElementById: (id) => mkEl(id), querySelectorAll: () => [],
                createElement: () => mkEl('t' + Object.keys(store).length), addEventListener() {} },
    window: { addEventListener() {} },
    fetch: async (url) => {
      calls.push(url);
      const pick = (o) => ({ ok: true, status: 200, json: async () => o });
      if (url.includes('/api/tree')) return pick({ ok: true, tree: { name: '', type: 'dir', children: [], files: [] } });
      if (url.includes('/api/health')) return pick({ ok: true, stats: { files: 1, chunks: 2, disk_bytes: 100 } });
      if (url.includes('/api/search')) return pick({ ok: true, mode: 'hybrid', hits: [] });
      return pick({ ok: true });
    },
    setTimeout: (f) => { try { f(); } catch (e) {} return 0; },
    clearTimeout: () => {},
    alert(){}, confirm: () => true, prompt: () => null, console,
    encodeURIComponent, decodeURIComponent, Error, JSON, String, Number, Promise, Object, Array,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: 'inline.js' });
  return { calls, sandbox, store };
}

(async () => {
  // 每个挂载点都要求请求前缀 = 挂载点（补上尾斜杠）。**红了就退出码非 0** ——
  // 以前这里只打印 ❌ 却不影响退出码，run_all 只看退出码，于是"59/59 全绿"里有一格是虚的
  // （2026-09-30 体检）。
  let bad = 0;
  for (const p of ['/notes/', '/', '/notes']) {
    const { calls } = runAt(p);
    await new Promise(r => setTimeout(r, 30));
    const want = p.endsWith('/') ? p : p + '/';
    const ok = calls.length > 0 && calls.every(u => u.startsWith(want));
    if (!ok) bad++;
    console.log('挂载 %-9s → 请求: %s  %s', p, JSON.stringify(calls), ok ? '✅ 前缀正确' : '❌ 前缀错误');
  }
  console.log(bad ? `失败：${bad} 个挂载点前缀不对` : '通过 3 / 3；失败：无');
  if (bad) process.exitCode = 1;
})();
