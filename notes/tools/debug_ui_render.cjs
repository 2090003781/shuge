// 用真实数据结构跑一遍前端渲染，定位"加载失败"的异常。
// 用法：node tools/debug_ui_render.cjs
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const http = require('node:http');

function get(pathname) {
  return new Promise((resolve, reject) => {
    http.get({ host: '127.0.0.1', port: 8790, path: pathname }, (res) => {
      let b = '';
      res.on('data', (c) => b += c);
      res.on('end', () => resolve(b));
    }).on('error', reject);
  });
}

(async () => {
  const html = await get('/notes/');
  const treeJson = JSON.parse(await get('/notes/api/tree'));
  const healthJson = JSON.parse(await get('/notes/api/health'));
  console.log('① 取到界面', html.length, '字节 | 树 ok=', treeJson.ok,
              '| 顶层目录数=', (treeJson.tree.children || []).length);

  // 构造更接近浏览器的 DOM（子节点要能容纳、className 要能设）
  function mkEl(tag) {
    const el = {
      tagName: tag, innerHTML: '', textContent: '', value: '', style: {}, dataset: {}, children: [],
      className: '', classList: { _s: new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);}, contains(c){return this._s.has(c);}, toggle(){} },
      appendChild(c) { this.children.push(c); return c; },
      addEventListener() {}, setAttribute() {}, getAttribute: () => null,
      querySelectorAll: () => [], focus() {},
      get firstElementChild() { return this.children[0] || { textContent: '' }; },
    };
    return el;
  }
  const byId = {};
  const sandbox = {
    location: { pathname: '/notes/' },
    document: {
      getElementById: (id) => (byId[id] = byId[id] || Object.assign(mkEl('div'), { id })),
      createElement: (t) => mkEl(t),
      querySelectorAll: () => [], addEventListener() {},
    },
    window: { addEventListener() {} },
    fetch: async (url) => {
      const pick = (o) => ({ ok: true, status: 200, json: async () => o });
      if (url.includes('/api/tree')) return pick(treeJson);
      if (url.includes('/api/health')) return pick(healthJson);
      return pick({ ok: true, hits: [] });
    },
    setTimeout: (f) => { try { f(); } catch (e) { console.log('   ⚠ setTimeout 内抛错:', e.message); } return 0; },
    clearTimeout: () => {},
    alert: (m) => console.log('   [alert]', m), confirm: () => true, prompt: () => null, console,
    encodeURIComponent, decodeURIComponent, Error, JSON, String, Number, Promise, Object, Array, RegExp,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);

  const code = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  try {
    vm.runInContext(code, sandbox, { filename: 'inline.js' });
    console.log('② 脚本执行: OK');
  } catch (e) {
    console.log('② 脚本执行抛错:', e.message, '\n', e.stack.split('\n').slice(0, 4).join('\n'));
  }

  await new Promise(r => setTimeout(r, 60));
  const tree = byId['tree'];
  console.log('③ tree.innerHTML 内容:', JSON.stringify((tree && tree.innerHTML || '').slice(0, 120)));
  console.log('   tree 子节点数:', tree ? tree.children.length : 0);
  console.log('   stats 文本:', byId['stats'] ? byId['stats'].textContent : '(无)');
})();
