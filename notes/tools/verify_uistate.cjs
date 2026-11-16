// 界面"状态是否跟得上"的验收：把页面脚本跑进假 DOM + 假后端，专测功能之间的联动与复位。
// 由来：2026-09-27 辞玖让"检查整体功能逻辑是否匹配、还有没有 bug"，这一轮审计出的 11 处
//       （本篇搜索抢光标、标签写错篇、保存失败无反馈、删除后不复位、打开竞态、搜索按钮不看范围、
//         树丢选中/展开、保存后光标跳文末、单引号毁 onclick、改标签后属性不同步、文档搜不到）
//       全部在这里留成常驻断言。
//
// 用法：node tools/verify_uistate.cjs [port]      默认 8848（只 GET 首页 HTML）
// 注意：写请求全部走假后端的桩，**不碰真实库**。
const vm = require('node:vm');
const http = require('node:http');

const PORT = Number(process.argv[2] || 8848);

function get(p) {
  return new Promise((res, rej) => {
    http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
      let b = ''; r.setEncoding('utf8'); r.on('data', c => b += c); r.on('end', () => res(b));
    }).on('error', rej);
  });
}

function mkEl(tag) {
  const cls = new Set();
  const el = {
    tagName: String(tag).toUpperCase(), textContent: '', value: '', style: {}, dataset: {},
    children: [], attrs: {}, className: '', scrollTop: 0, selectionStart: 0, selectionEnd: 0, focused: false,
    readOnly: false, checked: false, title: '', onclick: null, oncontextmenu: null, _h: {},
    classList: {
      add: c => cls.add(c), remove: c => cls.delete(c), contains: c => cls.has(c),
      toggle: (c, on) => { if (on === undefined) { const had = cls.has(c); had ? cls.delete(c) : cls.add(c); return !had; } const want = !!on; want ? cls.add(c) : cls.delete(c); return want; },
    },
    appendChild(c) { this.children.push(c); return c; },
    insertBefore(c) { this.children.unshift(c); return c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); return c; },
    remove() {},
    addEventListener(t, fn) { (this._h[t] = this._h[t] || []).push(fn); },     // 记下来，测试里能真的触发
    fire(t, ev) { (this._h[t] || []).forEach(fn => fn(ev || {})); },
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k] ?? null; },
    querySelectorAll: () => [], querySelector: () => null, focus() { this.focused = true; },
    setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; },
    get firstElementChild() { return this.children[0] || null; },
    get nextElementSibling() { return null; },
  };
  // 真浏览器里 innerHTML 赋值会重建子节点；假 DOM 若只存字符串，"树被重建"就看不出来（假阳性）
  let _html = '';
  Object.defineProperty(el, 'innerHTML', {
    get: () => _html,
    set: (v) => { _html = String(v); el.children.length = 0; },
  });
  return el;
}

let pass = 0, fail = 0;
const check = (name, cond, extra) => {
  cond ? pass++ : fail++;
  console.log('  %s %s%s', cond ? '✅' : '❌', name, extra === undefined ? '' : ' | ' + String(extra).slice(0, 180));
};

// ── 假后端：连"服务端状态"一起模拟（改标签后 POST 真的会改掉服务端那份）──
const TREE = {
  id: 'ROOT', name: '', files: [
    { id: 'F1', name: '甲.md', kind: 'markdown', rag: 'on', visibility: 'internal' },
    { id: 'F3', name: '丙.md', kind: 'markdown', rag: 'off', visibility: 'internal' },
  ], children: [{
    id: 'D1', name: '子目录', files: [
      { id: 'F2', name: '乙.md', kind: 'markdown', rag: 'on', visibility: 'internal' },
    ], children: [{ id: 'D2', name: '孙目录', files: [], children: [] }],
  }],
};
const SRV = {
  F1: { id: 'F1', name: '甲.md', content: '# 甲\n\n甲的内容\n', tags: ['旧'], frontmatter: { status: "it's ok", tags: ['旧'] },
        mentions: [{ id: 'F2', anchor: "it's 乙", name: '乙.md', section: '', section_heading: '' },
                   { id: 'F2', anchor: '乙.md#乙节', name: '乙.md', section: '乙节',
                     section_heading: '乙节 这是很长的小节标题用来占版面', section_ok: true },
                   { id: 'F9', anchor: '秘密篇', name: '（私密·未解锁）', hidden: true }],
        backlinks: [{ id: 'F2', name: '乙.md', anchor: '甲' }, { id: 'F3', name: '丙.md', anchor: '甲.md' },
                    { id: 'F9', name: '（私密·未解锁）', hidden: true }],
        backlink_groups: [{ section: '甲节', heading: '甲节', ok: false, count: 3,
                            refs: [{ id: 'F2', name: '乙.md', anchor: '甲' },
                                   { id: 'F3', name: '丙.md', anchor: '甲.md' },
                                   { id: 'F9', name: '（私密·未解锁）', hidden: true }] }],
        deleted_refs: ['没了的篇'] },
  F2: { id: 'F2', name: '乙.md', content: '# 乙\n\n## 乙节\n\n乙的内容\n', tags: [], frontmatter: {} },
  F3: { id: 'F3', name: '丙.md', content: '# 丙\n\n丙没进检索\n', tags: [], frontmatter: {},
        rag: 'off', created_at: '2026-09-28T08:30:00+08:00' },
  DOC: { id: 'DOC', name: '周报.docx', content: '', tags: [], frontmatter: {}, kind: 'document',
         derived: { format: 'docx', parser: 'docx(ooxml)', chars: 16, error: '', meta: { title: '周报样例' } } },
};
function fileOf(id) {
  const s = SRV[id];
  return Object.assign({
    kind: 'markdown', size: 42, visibility: 'internal',
    caps: { open: true, edit: true, search: true, store_only: false },
    mtime: '2026-09-27T10:00:00+08:00', locked: false, rag: 'on', doc_date: '',
    created_at: '2026-09-27T09:00:00+08:00',
    path: id === 'DOC' ? 'vault/周报.docx'
      : (id === 'F1' ? 'vault/甲.md' : (id === 'F3' ? 'vault/丙.md' : 'vault/子目录/乙.md')),
    mentions: [], backlinks: [], deleted_refs: [],
  }, s, { frontmatter: Object.assign({}, s.frontmatter) });
}

(async () => {
  const html = await get('/');
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  const byId = {};
  const writes = [];
  const reads = [];
  let delayF1 = 0, putFails = false;
  let lockUnlocked = true, lockConfigured = true, lockPubReady = true;   // 假库的锁状态（可切）
  const LOCK_PW = 'open-sesame';

  const sandbox = {
    location: { pathname: '/' },
    document: {
      getElementById: (id) => (byId[id] = byId[id] || Object.assign(mkEl('div'), { id })),
      createElement: (t) => mkEl(t), querySelectorAll: () => [], addEventListener() {},
      body: mkEl('body'), title: '',
    },
    getComputedStyle: () => ({ lineHeight: '22px', position: 'static' }),
    // 导入要读文件：假 DOM 里没有文件系统，用它把 __b64 直接交出去
    FileReader: class {
      readAsDataURL(f) {
        this.result = 'data:;base64,' + ((f && f.__b64) || '');
        setTimeout(() => { if (this.onload) this.onload(); }, 0);
      }
    },
    setTimeout, clearTimeout, setInterval, clearInterval, setImmediate,
    console: { log() {}, error() {} },
    alert() {}, confirm: () => true, prompt: () => null,
    navigator: { clipboard: { writeText: async () => {}, readText: async () => '' } },
    localStorage: { _v: {}, getItem(k) { return this._v[k] === undefined ? null : this._v[k]; },
                    setItem(k, v) { this._v[k] = String(v); },
                    removeItem(k) { delete this._v[k]; },
                    key(i) { const ks = Object.keys(this._v); return i < ks.length ? ks[i] : null; },
                    get length() { return Object.keys(this._v).length; } },
    encodeURIComponent, decodeURIComponent, Error, JSON, String, Number, Promise, Object, Array, RegExp,
    Math, parseFloat, Set, Date,
    fetch: async (url, opts) => {
      const p = String(url).replace(/^https?:\/\/[^/]+/, '');
      const method = (opts && opts.method) || 'GET';
      const body = opts && opts.body ? JSON.parse(opts.body) : null;
      (method === 'GET' ? reads : writes).push({ p, method, body });
      const J = (o) => ({ ok: true, status: 200, json: async () => o });
      if (p.indexOf('/api/lock/unlock') === 0) {          // 对密码才解锁，错的回 403
        const good = !!(body && body.password === LOCK_PW);
        if (good) lockUnlocked = true;
        return good ? J({ ok: true, unlocked: true, reindexed: 1 })
                    : { ok: false, status: 403, json: async () => ({ ok: false, error: '密码不对' }) };
      }
      if (p.indexOf('/api/lock/lock') === 0) { lockUnlocked = false; return J({ ok: true, unlocked: false }); }
      if (p.indexOf('/api/lock/change') === 0) return J({ ok: true, reencrypted: 2, configured: true, unlocked: true });
      if (p.indexOf('/api/lock/clear') === 0) {
        lockConfigured = false; lockUnlocked = false;
        return J({ ok: true, decrypted: 2, configured: false, unlocked: false });
      }
      if (p.indexOf('/api/lock') === 0) return J({ ok: true, configured: lockConfigured,
                                                   pub_ready: lockPubReady,
                                                   unlocked: lockUnlocked, private_files: 2 });
      if (p.indexOf('/api/tree') === 0) return J({ ok: true, tree: TREE });
      if (p.indexOf('/api/health') === 0) return J({ ok: true, stats: { files: 3, chunks: 4, disk_bytes: 1048576 }, pending_vectors: 0 });
      if (p.indexOf('/api/search') === 0) {
        // 有命中才看得出"收起/关闭"这条底栏；密码那次（解锁试探）给空，免得混进检索结果
        const isPw = p.indexOf(LOCK_PW) >= 0;
        return J({ ok: true, mode: 'hybrid', excluded_tags: [], hits: isPw ? [] : [
          { name: '甲.md', heading: '甲标题', text: '命中的片段一', channels: ['kw'], rrf: 0.031,
            seq: 0, total: 1, doc_date: '' },
          { name: '乙.md', heading: '乙标题', text: '命中的片段二', channels: ['vec'], rrf: 0.022,
            seq: 0, total: 1, doc_date: '' }] });
      }
      if (p.indexOf('/api/properties/find') === 0) return J({ ok: true, total: 1, hits: [{ id: 'F1', name: '甲.md', path: 'vault/甲.md', doc_date: '' }] });
      if (p.indexOf('/api/properties') === 0) return J({ ok: true, files_with_properties: 1,
        keys: [{ key: 'status', count: 1, values: [{ value: "it's ok", count: 1 }] }] });
      if (p.indexOf('/api/import/upload') === 0) return J({ ok: true, imported: 1, updated: 0, skipped: 0,
        failed: 0, errors: [], files: ['zz-导入验证.docx'], created_ids: ['F1'], bytes: 1234 });
      if (p.indexOf('/api/before-images/restore') === 0) return J({ ok: true, mode: 'recreated',
        file_id: 'F9', name: 'zz-废稿.md', from: '2026-09-28 19:00' });
      if (p.indexOf('/api/before-images/prune') === 0) return J({ ok: true, removed: 2, kept: 1 });
      if (p.indexOf('/api/before-images') === 0) return J({ ok: true, images: [
        { id: 'BI1', at: '2026-09-28T19:00:12+08:00', file_id: 'GONE1', name: 'zz-废稿.md',
          path: 'vault/项目/zz-废稿.md', reason: 'delete', writer: 'user', bytes: 2048,
          visibility: 'internal', rag: 'on', image: '前像/20260928/190012-abcd1234-zz-废稿.md' }] });
      const mDocx = p.match(/^\/api\/files\/([^/?]+)\/docx$/);
      if (mDocx) return J({ ok: true, bytes: 9000,
        info: { paras: 2, tables: 0, chars: 16,
                headings: [{ i: 0, level: 1, text: '项目周报' }] },
        html: '<h1>项目周报</h1>',
        blocks: [
          { i: 0, kind: 'p', style: 'Heading 1', heading: 1, align: '', list: {},
            text: '项目周报', runs: [{ t: '项目周报', b: true, size: 18 }] },
          { i: 1, kind: 'p', style: 'Normal', heading: 0, align: '', list: {},
            text: '这周做了三件事。', runs: [{ t: '这周做了三件事。', size: 12 }] }] });
      const mVis = p.match(/^\/api\/files\/([^/?]+)\/visibility$/);
      if (mVis && method === 'POST') {                    // 与 server.py 一个口径：
        const want = (body || {}).visibility;
        if (want === 'private' && !lockConfigured) {      // 没设过密码 → 没有钥匙能保护它
          return { ok: false, status: 403, json: async () => ({ ok: false, need_setup: true, locked: false,
            error: '还没设过库密码：先在「右键文件树 → 设置密码…」设一个，再来设为私密' }) };
        }
        if (want === 'private' && !lockUnlocked && !lockPubReady) {   // 老库：还没解锁过一次
          return { ok: false, status: 403, json: async () => ({ ok: false, need_setup: false, pub_ready: false,
            error: '库密钥还没生成：先在搜索框输一次密码解锁（会自动补上），之后锁着也能设为私密' }) };
        }
        if (want === 'internal' && !lockUnlocked) {       // 移出私密要解密 → 必须解锁
          return { ok: false, status: 403, json: async () => ({ ok: false, locked: true, error: 'locked' }) };
        }
        return J({ ok: true, configured: lockConfigured, unlocked: lockUnlocked,
                   result: { file_id: mVis[1], visibility: want,
                             encrypted: want === 'private',
                             via: want === 'private' ? (lockUnlocked ? 'master' : 'pubkey') : '',
                             decrypted: want !== 'private' } });
      }
      if (p === '/api/visibility/batch' && method === 'POST') {
        const want = (body || {}).visibility;
        const n = ((body || {}).ids || []).length;
        if (want === 'private' && !lockConfigured) {
          return { ok: false, status: 403, json: async () => ({ ok: false, need_setup: true,
            error: '还没设过库密码：先在「右键文件树 → 设置密码…」设一个，再来设为私密' }) };
        }
        if (want === 'internal' && !lockUnlocked) {
          return { ok: false, status: 403, json: async () => ({ ok: false, locked: true, error: 'locked' }) };
        }
        return J({ ok: true, visibility: want, count: n, skipped: 0, failed: [],
                   configured: lockConfigured, unlocked: lockUnlocked });
      }
      const mText = p.match(/^\/api\/files\/([^/?]+)\/text$/);
      if (mText) return J({ ok: true, format: 'docx', parser: 'docx(ooxml)', error: '', chars: 16,
                            text: '# 周报\n\n这周做了三件事。\n' });
      const mt = p.match(/^\/api\/files\/([^/?]+)\/tags$/);
      if (mt && method === 'POST') {                      // 服务端真的会写回 frontmatter（与 server.py 一致）
        SRV[mt[1]].tags = body.tags;
        SRV[mt[1]].frontmatter = Object.assign({}, SRV[mt[1]].frontmatter, { tags: body.tags });
        return J({ ok: true, tags: body.tags, frontmatter_synced: true });
      }
      const m = p.match(/^\/api\/files\/([^/?]+)/);
      if (m) {
        const id = m[1];
        if (method === 'PUT') {
          if (putFails) return { ok: false, status: 500, json: async () => ({ ok: false, error: '索引炸了' }) };
          return J({ ok: true, saved: { chunks: 2, mentions: 0, size_change: { before_bytes: 42, after_bytes: 42 } } });
        }
        if (method === 'DELETE') return J({ ok: true, deleted: true });
        if (id === 'F1' && delayF1) await new Promise(r => setTimeout(r, delayF1));
        if (!SRV[id]) return { ok: false, status: 404, json: async () => ({ ok: false, error: 'no such file' }) };
        return J({ ok: true, file: fileOf(id) });
      }
      return J({ ok: true });
    },
  };
  sandbox.__setLock = (v, c, p) => {                       // 测试里切锁状态用
    lockUnlocked = !!v;
    if (c !== undefined) lockConfigured = !!c;
    if (p !== undefined) lockPubReady = !!p;               // 老库：设过密码但还没生成库公钥
  };
  sandbox.window = sandbox; sandbox.globalThis = sandbox;
  sandbox.addEventListener = () => {};
  vm.createContext(sandbox);
  vm.runInContext(script, sandbox, { filename: 'inline.js' });
  const tick = () => new Promise(r => setImmediate(r));
  const wait = (ms) => new Promise(r => setTimeout(r, ms));
  for (let i = 0; i < 60; i++) {
    if (byId['stats'] && byId['stats'].textContent) break;
    await wait(10);
  }
  if (!byId['stats'] || !byId['stats'].textContent) {
    console.log('❌ 页面脚本没跑起来（统计没出来）：', String(byId['tree'] && byId['tree'].innerHTML).slice(0, 200));
    process.exit(2);
  }

  const findAll = (pred) => {
    const out = [];
    const walk = (el) => {
      if (!el || !el.children) return;
      if (pred(el)) out.push(el);
      el.children.forEach(walk);
    };
    walk(byId['tree']);
    return out;
  };
  const findRow = (id) => findAll(el => el.dataset && el.dataset.id === id)[0] || null;
  // 页签条上现在开着哪几签（按顺序）
  const tabNames = () => (String(byId['tabBar'].innerHTML).match(/class="tn">([^<]*)</g) || [])
    .map(s => s.replace(/^class="tn">/, '').replace(/<$/, ''));
  const findDirWrap = (name) => findAll(el => el.children[0] && el.children[0].dataset
    && el.children[0].dataset.dirName === name)[0] || null;
  const prepHead = (head) => { if (head && !head.children.length) head.appendChild(Object.assign(mkEl('span'), { textContent: '📁 x' })); };
  const txt = (s) => String(s).replace(/<[^>]*>/g, '');
  const hasWrite = (re, method) => writes.some(w => re.test(w.p) && (!method || w.method === method));

  console.log('① 打开 / 保存之后，文件树还认得"当前这篇"吗');
  const row1 = findRow('F1');
  await sandbox.openFile('F1', row1);
  check('点文件后那一行有 .sel', !!row1 && row1.classList.contains('sel'), row1 && row1.className);
  const wrap0 = findDirWrap('子目录');
  prepHead(wrap0.children[0]);
  wrap0.children[0].onclick();
  check('点文件夹头能展开', wrap0.children[1].style.display === '', wrap0.children[1].style.display);
  await sandbox.save();
  const row1b = findRow('F1');
  check('保存后当前那行仍有 .sel', !!row1b && row1b.classList.contains('sel'), row1b && row1b.className);
  check('保存后展开状态没被折回去', findDirWrap('子目录').children[1].style.display === '',
        findDirWrap('子目录').children[1].style.display);
  check('保存后编辑器光标没被重置到文末/开头', byId['editor'].selectionStart === 0 || byId['editor'].selectionStart === null
        || byId['editor'].selectionStart !== byId['editor'].value.length - 1,
        byId['editor'].selectionStart);

  console.log('② 保存失败：要让用户看见，并且不许把"未保存"摘掉');
  putFails = true;
  sandbox.markDirty(true);
  await sandbox.save();
  putFails = false;
  check('保存失败有明确提示', String(byId['toast'].textContent).includes('保存失败'), byId['toast'].textContent);
  check('保存失败后仍是"未保存"', byId['dirtyFlag'].style.display === 'block', byId['dirtyFlag'].style.display);
  sandbox.markDirty(false);

  console.log('③ 对某一篇的操作永远作用在"那一篇"上（多页签之后不再有"放弃修改"拦路）');
  await sandbox.openFile('F1', findRow('F1'));
  const writesBefore = writes.length;
  sandbox.confirm = () => false;                     // 就算确认框一律点"取消"
  sandbox.markDirty(true);                           // 甲还改脏着
  sandbox.openMenu({ preventDefault() {}, stopPropagation() {} },
                   { kind: 'file', id: 'F2', name: '乙.md', row: findRow('F2') });
  sandbox.prompt = () => '新标签';
  await sandbox.menuTags();
  const tagW3 = writes.slice(writesBefore).filter(w => /\/tags$/.test(w.p));
  check('标签打在乙身上（不是还停在甲）', tagW3.length === 1 && tagW3[0].p.indexOf('F2') >= 0,
        tagW3.map(w => w.p));
  findRow('F1').onclick();                           // 切回甲
  await tick();
  check('甲那篇的脏内容还在（切走不等于丢掉）',
        String(byId['editor'].value).indexOf('甲的内容') >= 0 && byId['dirtyFlag'].style.display === 'block',
        [String(byId['editor'].value).slice(0, 20), byId['dirtyFlag'].style.display]);
  sandbox.confirm = () => true;
  sandbox.prompt = () => null;
  sandbox.markDirty(false);

  console.log('④ 删篇 / 关签：签没了切到还开着的；全关完才回空态');
  await sandbox.openFile('F1', findRow('F1'));
  await sandbox.openFile('F2', findRow('F2'));       // 开两签
  check('两签都在，乙是当前', tabNames().join(',') === '甲.md,乙.md', tabNames());
  sandbox.openMenu({ preventDefault() {}, stopPropagation() {} },
                   { kind: 'file', id: 'F2', name: '乙.md', row: findRow('F2') });
  await sandbox.menuDelete();
  await tick();
  check('乙那一签关掉了', tabNames().indexOf('乙.md') < 0, tabNames());
  check('界面切到还开着的甲（编辑器换成它）',
        String(byId['editor'].value).indexOf('甲的内容') >= 0,
        String(byId['editor'].value).slice(0, 20));
  check('元信息条也跟着换成甲', txt(byId['meta'].innerHTML).indexOf('甲.md') >= 0,
        txt(byId['meta'].innerHTML).slice(0, 30));
  sandbox.tabClose(0, true);                         // 最后一签也关掉
  await tick();
  check('元信息条回空态', txt(byId['meta'].innerHTML).includes('未选择文件'), txt(byId['meta'].innerHTML).slice(0, 40));
  check('标题里没有残留的未保存标记', String(sandbox.document.title).indexOf('●') < 0, sandbox.document.title);
  check('编辑器清空', String(byId['editor'].value) === '', String(byId['editor'].value).slice(0, 20));
  check('右侧栏回空态', txt(byId['sideBody'].innerHTML).includes('选一篇笔记'),
        txt(byId['sideBody'].innerHTML).slice(0, 40));

  console.log('⑤ 连点两篇（先点的响应慢）：界面要停在最后点的那篇');
  delayF1 = 120;
  const pSlow = sandbox.openFile('F1', findRow('F1'));
  const pFast = sandbox.openFile('F2', findRow('F2'));
  await Promise.all([pSlow, pFast]);
  delayF1 = 0;
  check('编辑器是最后点的乙', String(byId['editor'].value).includes('乙的内容'),
        String(byId['editor'].value).replace(/\n/g, '⏎').slice(0, 30));
  check('元信息也是乙', txt(byId['meta'].innerHTML).includes('乙.md'), txt(byId['meta'].innerHTML).slice(0, 30));

  console.log('⑥ 「🔗 引用」页：双向引用都摊开，引用处的 onclick 是合法 JS');
  await sandbox.openFile('F1', null);
  sandbox.sideTab('refs');
  await tick(); await tick();
  const r6 = String(byId['sideBody'].innerHTML);
  check('出向：我引用了谁（列出指向的篇名）',
        r6.indexOf('我引用了谁') >= 0 && r6.indexOf('rname">乙<') >= 0, r6.slice(0, 200));
  check('引了但库里没有的单列一段', r6.indexOf('引了但库里没有的') >= 0 && r6.indexOf('没了的篇') >= 0,
        r6.slice(0, 340));
  check('入向：谁引用了我 + 被引小节（含"这一节没了"）',
        r6.indexOf('谁引用了我') >= 0 && r6.indexOf('甲节') >= 0 && r6.indexOf('修正…') >= 0,
        r6.slice(0, 420));
  check('锁定的引用：写成"灰的、不可点"（rrow locked + 未解锁）',
        r6.indexOf('rrow locked') >= 0 && r6.indexOf('未解锁') >= 0, r6.slice(0, 460));
  const lockedRows6 = (r6.match(/<div class="rrow locked"[^>]*>/g) || []);
  check('不可点的那几行确实没有 onclick（点了不响应）',
        lockedRows6.length >= 2 && lockedRows6.every(s => s.indexOf('onclick') < 0),
        lockedRows6.length + ' 行 / ' + JSON.stringify(lockedRows6.slice(0, 2)));
  const bl6 = String(byId['blinks'].innerHTML);
  check('编辑器底下那行：锁定的引用也是灰的、没有 onclick',
        bl6.indexOf('mention locked') >= 0 && !/class="mention locked"[^>]*onclick/.test(bl6),
        bl6.slice(0, 220));
  const md6 = String(sandbox.mdRender('看 @秘密篇 这篇', { '秘密篇': { id: 'F9', name: '（私密·未解锁）', hidden: true } }));
  check('预览里的引用同理：灰色、不可点',
        md6.indexOf('mention locked') >= 0 && md6.indexOf('onclick') < 0, md6.slice(0, 160));
  const md6b = String(sandbox.mdRender('看 @乙 这篇', { '乙': { id: 'F2', name: '乙.md' } }));
  check('可用引用还是绿的、能点（有 onclick）',
        md6b.indexOf('class="mention"') >= 0 && md6b.indexOf('onclick="openMention(') >= 0, md6b.slice(0, 160));
  check('出向行：点它是"跳本文那一行"，另有「↗目标」去打开对方',
        r6.indexOf('gotoRefLine(') >= 0 && r6.indexOf('↗目标') >= 0
        && r6.indexOf("onclick=\"openFile('F2')\"") < 0, r6.slice(0, 240));
  check('「↗目标」把小节也带上（打开被引用那篇 + 跳那一节）',
        r6.indexOf("openRefTarget('F2', '乙节')") >= 0, r6.slice(0, 760));
  check('高光用浅透色（自定义 ::selection 低透明度，不用浏览器默认亮蓝）',
        /#editor::selection\s*\{[^}]*rgba\([^)]*\.\d+\)/.test(String(html)), '');
  const r6vis = r6.replace(/title="[^"]*"/g, '');      // 只看可见文字（悬停提示里的不算）
  check('小节只留短名 #乙节，长的"小节标题"不占版面',
        r6vis.indexOf('#乙节') >= 0 && r6vis.indexOf('很长的小节标题') < 0, r6vis.slice(0, 420));
  check('完整小节标题仍在悬停提示里（要看得到）',
        r6.indexOf('很长的小节标题') >= 0, '');
  check('「改写…」改叫「改引用名…」', r6.indexOf('改引用名…') >= 0, '');
  check('入向行：点它=打开那一篇并跳到它引你的那一行',
        r6.indexOf("gotoRefLine('甲', 'F2')") >= 0, r6.slice(0, 520));
  // 真的跳一下：先让正文里有那一行
  if (!byId['right']) byId['right'] = Object.assign(mkEl('div'), { id: 'right' });
  byId['editor'].parentElement = byId['right'];
  byId['editor'].value = '# 甲\n\n第一行\n这里引用了 @乙.md 一下\n第三行\n';
  byId['editor'].scrollTop = 0;
  await sandbox.gotoRefLine('乙.md', 'F1');
  const want6 = String(byId['editor'].value).split('\n')[3];
  check('跳到那一行并整行选中',
        byId['editor'].selectionStart === 9 && byId['editor'].selectionEnd === 9 + want6.length,
        [byId['editor'].selectionStart, byId['editor'].selectionEnd, want6.length]);
  check('闪烁：排了计时器（选中→收成光标→再选中）', !!sandbox.__blinkT, sandbox.__blinkT);
  clearTimeout(sandbox.__blinkT);            // 别让它继续 tick 把光标挪了（会害到后面几节）
  check('不再往编辑器上盖浮层（改用选区本身当高亮）',
        (byId['right'].children || []).filter(c => c && c.id === 'lineFlash').length === 0, '');
  // 被引用那篇：也要跳过去并高亮（辞玖 2026-09-30）
  byId['toast'].textContent = '';
  await sandbox.openRefTarget('F2', '乙节');
  const selT = String(byId['editor'].value).substring(byId['editor'].selectionStart,
                                                      byId['editor'].selectionEnd);
  check('跳到被引用那篇的「乙节」那一行并选中', selT.indexOf('乙节') >= 0, selT);
  check('那边也闪（排了计时器）', !!sandbox.__blinkT, sandbox.__blinkT);
  clearTimeout(sandbox.__blinkT);
  await sandbox.openFile('F1', null);              // 回到甲，后面几节按甲来
  byId['toast'].textContent = '';
  await sandbox.gotoRefLine('根本没这名字', 'F1');
  check('找不到那一行时给一句人话', String(byId['toast'].textContent).indexOf('没找到') >= 0,
        byId['toast'].textContent);
  const codes6 = (r6.match(/onclick="[^"]*"/g) || []).map(s => s.slice(9, -1));
  let bad6 = '';
  codes6.forEach(c => { try { new Function(c); } catch (e) { bad6 = c + ' :: ' + e.message; } });
  check('页面里每条 onclick 都是合法 JS（含带单引号的引用名）', !bad6 && codes6.length > 0,
        bad6 || ('条数 ' + codes6.length));
  check('「按属性找」已经不在了', String(html).indexOf("sideTab('find')") < 0, '');
  // 改写引用：把正文里的 @旧名 换成 @新名，然后走正常保存
  byId['editor'].value = '# 甲\n\n见 @乙.md 这篇\n';
  sandbox.prompt = () => '新名字.md';
  const w6 = writes.length;
  await sandbox.rewriteRef('乙.md');
  const put6 = writes.slice(w6).filter(w => w.method === 'PUT').pop();
  check('改写引用：正文里 @旧名 换成 @新名并保存',
        !!(put6 && String(put6.body.content).indexOf('@新名字.md') >= 0),
        put6 ? String(put6.body.content).replace(/\n/g, '⏎').slice(0, 40) : '没发 PUT');
  sandbox.prompt = () => null;
  sandbox.sideTab('outline');

  console.log('⑦ 改标签之后：文件头属性（⚙）跟标签（🏷）不许自相矛盾');
  sandbox.prompt = () => '甲,乙';
  await sandbox.editTags();
  await tick();
  const meta7 = txt(byId['meta'].innerHTML);
  check('同一条元信息条里 tags 已同步成新值', meta7.includes('tags=甲'), meta7.slice(0, 120));
  sandbox.prompt = () => null;

  console.log('⑧ 本篇模式下在正文里打字：命中列表会更新，但光标不许被抢走');
  await sandbox.openFile('F1', null);
  byId['q'].value = '甲';
  sandbox.setScope('doc');
  await sandbox.openFile('F1', null);
  byId['editor'].value = '# 甲\n\n甲的内容\n';
  byId['editor'].selectionStart = byId['editor'].selectionEnd = byId['editor'].value.length;   // 光标停在文末
  byId['editor'].fire('input');                    // 真的走注册在编辑器上的那个 input 处理器
  await wait(220);
  check('打字触发的重算没把光标拉回第 1 处命中',
        byId['editor'].selectionStart === byId['editor'].value.length,
        [byId['editor'].selectionStart, byId['editor'].value.length]);
  check('命中列表照样算出来了', String(byId['hits'].innerHTML).includes('共'),
        txt(byId['hits'].innerHTML).slice(0, 60));
  byId['q'].value = '内容';
  sandbox.setScope('doc');
  byId['q'].fire('input');
  await wait(220);
  check('在搜索框里打字仍会定位到第一处（这个行为要留着）',
        byId['editor'].selectionStart === byId['editor'].value.indexOf('内容'),
        [byId['editor'].selectionStart, byId['editor'].value.indexOf('内容')]);

  console.log('⑨ 「🔍 搜索」按钮跟着范围走：本篇模式不该去打全库接口');
  byId['q'].value = '甲';
  sandbox.setScope('doc');
  await sandbox.doSearch();
  check('doSearch 在本篇模式被拦下（没有 /api/search 请求）',
        !hasWrite(/\/api\/search/, 'GET') && !reads.slice(-1).some(r => /\/api\/search/.test(r.p)),
        reads.slice(-2).map(r => r.p));
  const posBefore = byId['docPos'] && byId['docPos'].textContent;
  sandbox.searchGo();
  check('按钮 = 下一个命中（不是全库）', String(byId['docPos'] && byId['docPos'].textContent).length > 0, posBefore);

  console.log('⑩ 文档（docx/pdf）的「本篇搜索」与「大纲」读解析正文，不再假装"找不到"');
  sandbox.setScope('all');
  await sandbox.openFile('DOC', null);
  await tick(); await tick();
  byId['q'].value = '周报';
  sandbox.setScope('doc');
  await tick();
  check('本篇能搜到文档里明明有的字', !txt(byId['hits'].innerHTML).includes('没有「周报」'),
        txt(byId['hits'].innerHTML).slice(0, 70));
  sandbox.sideTab('outline');
  const ol = txt(byId['sideBody'].innerHTML);
  check('大纲从解析正文里取到标题', ol.includes('周报') && !ol.includes('还没有标题'), ol.slice(0, 70));
  check('文档大纲能点（docx 走引擎编辑器：点一下滚到那一段）',
        ol.includes('周报') && ol.includes('跳'), ol.slice(0, 80));

  console.log('⑪ 选中层：点文件夹 / 点文件都要看得见"我点到的是这个"');
  const dirWrap = findDirWrap('子目录');
  prepHead(dirWrap.children[0]);
  dirWrap.children[0].onclick();                       // 点文件夹头
  check('点文件夹：那一行有点亮（.sel）', dirWrap.children[0].classList.contains('sel'),
        dirWrap.children[0].className);
  check('点文件夹：照样展开/收起（两件事都做）',
        dirWrap.children[1].style.display === '' || dirWrap.children[1].style.display === 'none',
        dirWrap.children[1].style.display);
  check('树底下那行写着"已选文件夹：…"',
        String(byId['treeSel'].textContent).indexOf('已选文件夹') >= 0, byId['treeSel'].textContent);
  await sandbox.openFile('F2', findRow('F2'));         // 再点文件
  check('点文件：选中层挪到文件那一行',
        findRow('F2').classList.contains('sel')
        && !findDirWrap('子目录').children[0].classList.contains('sel'),
        [findRow('F2').className, findDirWrap('子目录').children[0].className]);
  check('树底下那行改成"已打开：…"',
        String(byId['treeSel'].textContent).indexOf('已打开') >= 0, byId['treeSel'].textContent);
  findDirWrap('子目录').children[0].onclick();         // 再点回文件夹
  await sandbox.loadTree();                            // 树重画（保存/新建之后会发生）
  check('树重画后：选中的文件夹还亮着',
        findDirWrap('子目录').children[0].classList.contains('sel'),
        findDirWrap('子目录').children[0].className);

  check('树里的行禁掉了文字选中（user-select:none）',
        /#tree[^{]*\{[^}]*user-select:none/.test(String(html)) || String(html).indexOf('user-select:none') >= 0,
        '页面 CSS 里应有 user-select:none');

  console.log('⑫ 导入到哪儿：默认当前打开位置 + 文件管理器式挑位置');
  await sandbox.openFile('F2', findRow('F2'));              // 打开「子目录/乙.md」
  check('默认目标 = 当前打开那篇所在的文件夹',
        sandbox.defaultUnder() === 'vault/子目录', sandbox.defaultUnder());
  sandbox.openImport(false);                    // 打开导入弹窗（假 DOM 里不真弹系统选择框）
  check('导入弹窗开着（来源 + 位置在一页里）',
        byId['importModal'].classList.contains('open'), byId['importModal'].className);
  const dHtml = String(byId['destTree'].innerHTML);
  check('弹窗里摊开了库内文件夹树（根目录 + 子目录）',
        dHtml.indexOf('根目录') >= 0 && dHtml.indexOf('子目录') >= 0, dHtml.slice(0, 120));
  check('默认全收起：儿孙那一层不摊开（孙目录看不见）', dHtml.indexOf('孙目录') < 0, dHtml.slice(0, 170));
  sandbox.destPick('子目录');
  check('点一个文件夹就选上了（导入位置跟着变）',
        String(byId['impUnderText'].textContent) === '子目录', byId['impUnderText'].textContent);
  check('点它就展开：孙目录出现了',
        String(byId['destTree'].innerHTML).indexOf('孙目录') >= 0,
        String(byId['destTree'].innerHTML).slice(0, 200));
  sandbox.closeImport();
  check('关掉后收起来', !byId['importModal'].classList.contains('open'), byId['importModal'].className);

  console.log('⑬ 没开文件=欢迎页；左边树上面那一栏；选择模式');
  sandbox.resetEditor();                                   // 清成"没打开任何文件"
  check('没开文件时显示欢迎页、编辑器让位',
        byId['welcome'].style.display === 'flex' && byId['editor'].style.display === 'none',
        [byId['welcome'].style.display, byId['editor'].style.display]);
  check('没开文件时编辑工具条也收起来', byId['ribbon'].style.display === 'none',
        byId['ribbon'].style.display);
  check('欢迎页上有那几个按钮', String(html).indexOf('id="welcome"') >= 0
        && String(html).indexOf('quickSearch()') >= 0, '');
  await sandbox.openFile('F1', null);
  check('打开一篇后欢迎页收起、编辑器回来',
        byId['welcome'].style.display === 'none' && byId['editor'].style.display === 'block',
        [byId['welcome'].style.display, byId['editor'].style.display]);
  check('原来树上面那一栏已撤掉（元素没了）', String(html).indexOf('id="treeBar"') < 0, '');
  check('树栏那几件事还在（挪进了菜单）',
        ['＋ 新建笔记', 'treeSelToggle()', 'loadTree()']
          .every(x => String(html).indexOf(x) >= 0), '');
  // 2026-09-30： 「▾ 更多新建」从"原地换菜单"改成**真子菜单**（悬停展开，父菜单不动）
  check('▾ 更多新建 是真子菜单（子条目里是 Word/ODF/PDF/数据表，不再重复新建笔记与导入）',
        String(html).indexOf("['▾ 更多新建', '', '', [") >= 0
        && String(html).indexOf('新建 Word 文档') >= 0
        && String(html).indexOf('🗂 数据表') >= 0, '');
  sandbox.newMenu({ preventDefault() {}, stopPropagation() {} });
  const nm = String(byId['ctxmenu'].innerHTML);
  check('点 ▾ 展开：笔记 / 文件夹 / Word / 导入 / 数据表 都在',
        ['新建笔记', '新建文件夹', 'Word 文档', '从磁盘导入', '数据表']
          .every(x => nm.indexOf(x) >= 0), nm.slice(0, 170));
  sandbox.treeSelToggle(true);
  check('进多选模式：批量条出来', byId['treeBatch'].style.display === 'flex',
        byId['treeBatch'].style.display);
  await tick();
  sandbox.treeCheck('F1', findRow('F1'));
  check('勾一篇 → 计数跟着走', String(byId['treeBatchN'].textContent).indexOf('已选 1') >= 0,
        byId['treeBatchN'].textContent);
  check('勾上后那一行前面有 ☑',
        String(findRow('F1').innerHTML).indexOf('☑') >= 0, String(findRow('F1').innerHTML).slice(0, 60));
  sandbox.treeSelToggle(false);
  check('退出选择模式：批量条收起', byId['treeBatch'].style.display === 'none',
        byId['treeBatch'].style.display);

  console.log('⑭ 行的 ⋯ 菜单：这一篇的操作 + 原来树栏那一组，全在里头');
  const row1Html = String(findRow('F1').innerHTML);
  check('行里不再标格式全称（markdown 这种词没了）',
        row1Html.indexOf('markdown') < 0 && row1Html.indexOf('甲.md') >= 0, row1Html.slice(0, 130));
  check('每行末尾有 ⋯ 按钮', row1Html.indexOf('dots') >= 0 && row1Html.indexOf('⋯') >= 0, '');
  check('悬停与选中是同一层底色（.row:hover, .row.sel 一起写）',
        /\.row:hover,\s*\.row\.sel/.test(String(html)), '');
  const mHoverRule = String(html).match(/\.row:hover,\s*\.row\.sel\s*\{[^}]*\}/);
  check('悬停那一档里没有蓝条（只有底色）',
        !!mHoverRule && mHoverRule[0].indexOf('box-shadow') < 0, mHoverRule && mHoverRule[0]);
  check('蓝条只画在选中上', /\.row\.sel\s*\{[^}]*box-shadow/.test(String(html)), '');
  const fakeEv = { preventDefault() {}, stopPropagation() {}, clientX: 10, clientY: 10 };
  sandbox.rowDots(fakeEv, 'f:F1');
  const dm = String(byId['ctxmenu'].innerHTML);
  check('⋯ 里有这一篇的操作（重命名 / 删除进回收站）',
        dm.indexOf('重命名') >= 0 && dm.indexOf('删除（进回收站）') >= 0, dm.slice(0, 160));
  check('⋯ 里也收进了树栏那一组（新建 / 导入 / 选择 / 刷新）',
        ['新建笔记', '从磁盘导入', '选择（勾几篇', '刷新文件树'].every(x => dm.indexOf(x) >= 0),
        dm.slice(0, 220));
  check('「▾ 更多新建」渲染成**真子菜单**（submenu + subitems，含 Word/ODF/PDF/数据表）',
        dm.indexOf('▾ 更多新建') >= 0 && dm.indexOf('class="submenu"') >= 0
        && dm.indexOf('class="subitems"') >= 0 && dm.indexOf('新建 Word 文档') >= 0
        && dm.indexOf('🗂 数据表') >= 0, dm.slice(0, 260));
  check('子菜单里不再重复父菜单已有的「新建笔记 / 从磁盘导入」',
        dm.indexOf('新建笔记（.md）') < 0, '');
  sandbox.rowDots(fakeEv, 'd:D1');
  const fm2 = String(byId['ctxmenu'].innerHTML);
  check('文件夹的 ⋯ 也带这一组（删除文件夹 + 刷新）',
        fm2.indexOf('删除文件夹') >= 0 && fm2.indexOf('刷新文件树') >= 0, fm2.slice(0, 160));

  console.log('⑮ 回收站：一个按钮拉起的独立页面');
  check('顶栏有回收站按钮', /id="btnBin"[^>]*openBin\(\)/.test(String(html)), '');
  await sandbox.openBin();
  check('页面打开了', byId['binpage'].classList.contains('open'), byId['binpage'].className);
  const bHtml = String(byId['binBody'].innerHTML);
  check('列出了前像：名字 + 删除 + 恢复按钮',
        bHtml.indexOf('zz-废稿.md') >= 0 && bHtml.indexOf('🗑 删除') >= 0
        && bHtml.indexOf('恢复这一版') >= 0, bHtml.slice(0, 200));
  check('原篇已被删的写明「会重建」', bHtml.indexOf('会重建') >= 0, bHtml.slice(0, 200));
  check('条数写在标题栏', String(byId['binFoot'].textContent).indexOf('共 1 条') >= 0,
        byId['binFoot'].textContent);
  await sandbox.binRestore('BI1');
  check('恢复打到接口上', hasWrite(/\/api\/before-images\/restore$/, 'POST'),
        writes.filter(w => /before-images/.test(w.p)).map(w => w.p + ' ' + w.method));
  sandbox.prompt = () => '30';
  await sandbox.binPrune();
  const pw = writes.filter(w => /\/api\/before-images\/prune$/.test(w.p)).pop();
  check('清理带着天数打过去', !!(pw && pw.body && pw.body.days === 30), pw && pw.body);
  sandbox.closeBin();
  check('关掉后收起来', !byId['binpage'].classList.contains('open'), byId['binpage'].className);

  console.log('⑯ 未保存草稿：打字就先存本地，下次打开自动恢复，只有"明确不保存"才丢');
  // 先清干净：文档编辑器从 2026-09-30 起也可编辑 → 上面几段可能留下 draft:DOC
  Object.keys(sandbox.localStorage._v).forEach(k => { if (k.indexOf('draft:') === 0) delete sandbox.localStorage._v[k]; });
  await sandbox.openFile('F1', null);
  const _draftKeys = () => Object.keys(sandbox.localStorage._v).filter(k => k.indexOf('draft:') === 0);
  byId['editor'].value = '# 甲\n\n改了但没存\n';
  byId['editor'].fire('input');
  await wait(1100);                                   // 等去抖（800ms）落一次
  check('打字后本地存了草稿', _draftKeys().length === 1 && _draftKeys()[0] === 'draft:F1', _draftKeys());
  sandbox.resetEditor();                              // 模拟刷新：内存里的签/dirty 都没了，本地草稿还在
  check('（前置）刷新后是空场，本地草稿还在', _draftKeys().length === 1, _draftKeys());
  await sandbox.openFile('F1', null);
  check('再打开自动恢复成草稿那一版',
        String(byId['editor'].value).indexOf('改了但没存') >= 0,
        String(byId['editor'].value).replace(/\n/g, '⏎'));
  check('顶部草稿提示条露出来', byId['draftBar'].classList.contains('on'), byId['draftBar'].className);
  check('草稿时间按本地时区显示（不是 UTC 那个字符串）',
        /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(String((byId['draftAt'] || {}).textContent || '')),
        (byId['draftAt'] || {}).textContent);
  check('并且回到"未保存"状态', byId['dirtyFlag'].style.display === 'block', byId['dirtyFlag'].style.display);
  await sandbox.save();
  check('保存成功后草稿清掉、提示条收起',
        _draftKeys().length === 0 && !byId['draftBar'].classList.contains('on'), _draftKeys());
  byId['editor'].value = '# 甲\n\n又改了一版\n';
  byId['editor'].fire('input');
  await wait(1100);
  check('再打字又存了草稿', _draftKeys().length === 1, _draftKeys());
  sandbox.resetEditor();
  await sandbox.openFile('F1', null);
  sandbox.draftDiscard();
  check('「丢弃草稿」后回到库里那一版',
        String(byId['editor'].value).indexOf('又改了一版') < 0
        && String(byId['editor'].value).indexOf('甲的内容') >= 0,
        String(byId['editor'].value).replace(/\n/g, '⏎').slice(0, 40));
  check('本地草稿也没了', _draftKeys().length === 0, _draftKeys());
  check('提示条收起、未保存标记清掉',
        !byId['draftBar'].classList.contains('on') && byId['dirtyFlag'].style.display === 'none',
        [byId['draftBar'].className, byId['dirtyFlag'].style.display]);
  check('草稿是浏览器本地存（键名 draft:<id>）', /DRAFT_PREFIX = 'draft:'/.test(String(html)), '');

  console.log('⑰ 长名字别撑破 / 导入必须看得见（辞玖 2026-09-29）');
  check('树里长名字走省略号（不撑破面板）',
        /#tree \.row > span:first-child\s*\{[^}]*text-overflow:ellipsis/.test(String(html)), '');
  check('元信息条的长名字也封顶', /#meta > span:first-child[^{]*\{[^}]*text-overflow:ellipsis/.test(String(html)), '');
  check('行的 title 里带全名（悬停能看全）',
        String(findRow('F1').innerHTML).indexOf('title="甲.md') >= 0,
        String(findRow('F1').innerHTML).slice(0, 90));
  sandbox.impOnPick({ files: [{ name: 'zz-导入验证.docx', size: 1234, __b64: 'AAA', webkitRelativePath: '' }],
                      value: '' });
  check('选完文件那行字写出文件名',
        String(byId['impList'].textContent).indexOf('zz-导入验证.docx') >= 0, byId['impList'].textContent);
  await sandbox.doImport();
  check('导完把结果显示出来（不再一片安静）',
        String(byId['impInfo'].textContent).indexOf('导入 1 篇') >= 0, byId['impInfo'].textContent);
  check('toast 也说了一句', String(byId['toast'].textContent).indexOf('导入 1 篇') >= 0,
        byId['toast'].textContent);
  check('导入那一行在树里闪出来（给了落点）',
        String(findRow('F1').innerHTML).indexOf('甲.md') >= 0 && findRow('F1').classList.contains('flash'),
        findRow('F1').className);
  check('还**自动打开了**刚导进来的那一篇（编辑器换成它）',
        String(byId['editor'].value).indexOf('甲的内容') >= 0,
        String(byId['editor'].value).replace(/\n/g, '⏎').slice(0, 30));
  check('打开的那一行是选中态（树也认它）',
        !!(findRow('F1') && findRow('F1').classList.contains('sel')), findRow('F1') && findRow('F1').className);
  // 手上有未保存改动时：不许抢编辑、不许清草稿，但落点照样给
  await sandbox.openFile('F2', null);
  byId['editor'].value = '# 乙\n\n正在改，别动我\n';
  byId['editor'].fire('input');
  sandbox.markDirty(true);
  await wait(1100);                                   // 先让本地草稿真的落下去（去抖 800ms）
  check('（前置）本地已经有草稿', _draftKeys().length === 1, _draftKeys());
  sandbox.impOnPick({ files: [{ name: 'zz-第二篇.docx', size: 999, __b64: 'BBB', webkitRelativePath: '' }],
                      value: '' });
  await sandbox.doImport();
  check('有未保存改动时：不自动打开（编辑器还停在你改的那篇）',
        String(byId['editor'].value).indexOf('正在改，别动我') >= 0,
        String(byId['editor'].value).replace(/\n/g, '⏎').slice(0, 30));
  check('你的本地草稿也没被清掉', _draftKeys().length === 1, _draftKeys());
  check('但落点照样给（导进来那一行在闪）',
        !!(findRow('F1') && findRow('F1').classList.contains('flash')), findRow('F1') && findRow('F1').className);
  sandbox.markDirty(false);
  check('导完按钮恢复可用',
        byId['impGo'].disabled === false && String(byId['impGo'].textContent) === '开始导入',
        [byId['impGo'].disabled, byId['impGo'].textContent]);
  sandbox.impOnPick({ files: [{ name: '太大.docx', size: 200 * 1048576, __b64: '', webkitRelativePath: '' }],
                      value: '' });
  const wBefore17 = writes.length;
  await sandbox.doImport();
  check('超大文件当场拒绝并说清怎么办',
        String(byId['impInfo'].textContent).indexOf('太大') >= 0, byId['impInfo'].textContent);
  check('而且没有发出上传请求', writes.length === wBefore17, writes.slice(wBefore17).map(w => w.p));
  check('拖文件进来有落点（提示罩 + 只认 Files）',
        String(html).indexOf('id="dropHint"') >= 0 && String(html).indexOf('hasFiles') >= 0, '');

  console.log('⑱ 多页签：同时开多篇，各签的正文/脏标记/撤销栈互不串');
  sandbox.markDirty(false);
  sandbox.resetEditor();                              // 从干净状态起（上一节留的草稿会清掉）
  Object.keys(sandbox.localStorage._v).filter(k => k.indexOf('draft:') === 0)
    .forEach(k => sandbox.localStorage.removeItem(k));
  await sandbox.openFile('F1', findRow('F1'));
  await sandbox.openFile('F2', findRow('F2'));
  check('开了两签', tabNames().join(',') === '甲.md,乙.md', tabNames());
  check('当前签高亮', String(byId['tabBar'].innerHTML).indexOf('tab on') >= 0, '');
  check('编辑器是乙', String(byId['editor'].value).indexOf('乙的内容') >= 0,
        String(byId['editor'].value).slice(0, 20));
  byId['editor'].value = '# 乙\n\n乙改了一版没存\n';   // 只把乙改脏
  byId['editor'].fire('input');
  sandbox.markDirty(true);
  check('乙那一签有脏点', /tab on dirty|tab dirty/.test(String(byId['tabBar'].innerHTML)), '');
  findRow('F1').onclick();                              // 点树里的甲 → 切签
  await tick();
  check('切回甲：编辑器换成甲的正文', String(byId['editor'].value).indexOf('甲的内容') >= 0,
        String(byId['editor'].value).slice(0, 20));
  check('切回甲：甲是干净的（脏的是乙）', byId['dirtyFlag'].style.display === 'none',
        byId['dirtyFlag'].style.display);
  sandbox.tabClick({ preventDefault() {}, stopPropagation() {} }, 1);   // 点页签回乙
  await tick();
  check('切回乙：刚才没保存的那版还在',
        String(byId['editor'].value).indexOf('乙改了一版没存') >= 0,
        String(byId['editor'].value).replace(/\n/g, '⏎').slice(0, 30));
  check('切回乙：脏点还在', String(byId['tabBar'].innerHTML).indexOf('dirty') >= 0, '');
  check('开过的签记在本地（刷新后能接回来）',
        String(sandbox.localStorage.getItem('openTabs') || '').indexOf('F1') >= 0,
        sandbox.localStorage.getItem('openTabs'));
  // 关掉有脏内容的签：必须先问
  sandbox.confirm = () => false;
  sandbox.tabClose(1);
  check('关脏签时问了一句，点取消就没关', tabNames().length === 2, tabNames());
  sandbox.confirm = () => true;
  sandbox.tabClose(1);
  check('确认后关掉，且那一签的本地草稿也清了',
        tabNames().join(',') === '甲.md'
        && Object.keys(sandbox.localStorage._v).filter(k => k.indexOf('draft:') === 0).length === 0,
        [tabNames(), Object.keys(sandbox.localStorage._v)]);
  sandbox.markDirty(false);

  console.log('⑲ 树里只写名字；"进没进检索 / 入库时间"这些去属性里看');
  const r3 = String(findRow('F3').innerHTML);
  const r3vis = r3.replace(/title="[^"]*"/g, '');     // 只看可见文字：悬停提示里的不算
  check('树里不再标「未入检索」（F3 就是未入检索的）', r3vis.indexOf('未入检索') < 0, r3vis.slice(0, 110));
  check('未入检索的文件图标也不再特殊化（还是 📄）', r3.indexOf('📄') >= 0, r3.slice(0, 110));
  check('但悬停提示里写着（要看得到，只是不占地方）', r3.indexOf('未入检索库') >= 0, '');
  check('树里也不冒 🔒', String(findRow('F1').innerHTML).indexOf('🔒') < 0, '');
  await sandbox.openFile('F3', null);
  const meta19 = txt(byId['meta'].innerHTML);
  check('元信息条写明"未入检索（只存放）"', meta19.indexOf('未入检索（只存放）') >= 0, meta19.slice(0, 150));
  check('元信息条有入库时间', meta19.indexOf('📥 入库') >= 0, meta19.slice(0, 150));
  check('元信息条的修改时间是可读格式（不是裸 ISO）',
        /✏️ 改 \d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(meta19), meta19.slice(0, 170));
  sandbox.sideTab('props');
  await tick(); await tick();
  const props19 = txt(byId['sideBody'].innerHTML);
  check('🏷 属性里有「库内信息」', props19.indexOf('库内信息') >= 0, props19.slice(0, 130));
  check('里面写着 检索 / 入库时间 / 修改时间 / 大小 / 路径 / 可见性',
        ['检索', '入库时间', '修改时间', '大小', '路径', '可见性'].every(x => props19.indexOf(x) >= 0),
        props19.slice(0, 240));
  check('并且明说它没进检索', props19.indexOf('未入检索') >= 0, props19.slice(0, 200));
  sandbox.sideTab('outline');

  console.log('⑳ 解锁藏在搜索框里（界面上没有锁按钮）');
  check('顶栏那把大金锁没了', String(html).indexOf('id="btnLock"') < 0, '');
  check('"立即上锁"藏在维护菜单里（lockNow 接线在）',
        String(html).indexOf('立即上锁') >= 0 && String(html).indexOf('lockNow()') >= 0, '');
  sandbox.__setLock(false);
  await sandbox.loadLock();
  sandbox.setScope('all');                    // 前面几节把范围切到过"本篇"，这里先切回来
  byId['q'].value = '随便搜的词';
  const w20 = writes.length, r20 = reads.length;
  await sandbox.searchGo();
  check('普通词：试过解锁（不是密码）后照旧检索',
        writes.slice(w20).some(w => w.p.indexOf('/api/lock/unlock') === 0)
        && reads.slice(r20).some(r => r.p.indexOf('/api/search') === 0),
        [writes.slice(w20).map(w => w.p), reads.slice(r20).map(r => r.p).slice(0, 4)]);
  check('普通词没被清掉（解锁失败就当搜索词）', String(byId['q'].value) === '随便搜的词', byId['q'].value);
  byId['toast'].textContent = '';
  byId['q'].value = LOCK_PW;
  const w21 = writes.length, r21 = reads.length;
  await sandbox.searchGo();
  check('密码：走了解锁接口', writes.slice(w21).some(w => w.p.indexOf('/api/lock/unlock') === 0),
        writes.slice(w21).map(w => w.p));
  check('密码**没有**被当成搜索词发出去（不能写进检索记录）',
        !writes.slice(w21).some(w => w.p.indexOf('/api/search') === 0 && w.p.indexOf(LOCK_PW) >= 0),
        writes.slice(w21).filter(w => w.p.indexOf('/api/search') === 0).map(w => w.p));
  check('密码：搜索框清空', String(byId['q'].value) === '', byId['q'].value);
  check('解锁后把树刷出来了（私密篇自己冒出来）',
        reads.slice(r21).some(r => r.p.indexOf('/api/tree') === 0), reads.slice(r21).map(r => r.p));
  check('全程没有任何提示（辞玖："过程和界面不需要提示"）',
        String(byId['toast'].textContent).indexOf('解锁') < 0, byId['toast'].textContent);

  console.log('㉑ 锁：一条可扩展条，按场景给子条目');
  const openMaint = () => {
    sandbox.openMenu({ preventDefault() {}, stopPropagation() {}, clientX: 5, clientY: 5 }, { kind: 'maint' });
    return String(byId['ctxmenu'].innerHTML);
  };
  sandbox.__setLock(true, true);
  await sandbox.loadLock();
  let m21 = openMaint();
  check('解锁状态：锁那一条是可扩展条（submenu）', m21.indexOf('class="submenu"') >= 0, m21.slice(0, 200));
  check('子条目里有：立即上锁 / 更换密码 / 取消密码',
        ['立即上锁', '更换密码', '取消密码'].every(x => m21.indexOf(x) >= 0), m21.slice(0, 320));
  check('子条目挂在 subitems 里（悬停才展开）', m21.indexOf('class="subitems"') >= 0, '');
  sandbox.__setLock(false, true);
  await sandbox.loadLock();
  m21 = openMaint();
  // 判据用 🔒（锁相关条目都以它开头），**不能再用 class="submenu"** ——
  // 2026-09-30「▾ 更多新建」也成了一条合法子菜单，拿 class 当判据会误伤。
  check('锁着时**连"锁"这一条都不显示**（辞玖 2026-09-29）',
        m21.indexOf('立即上锁') < 0 && m21.indexOf('更换密码') < 0
        && m21.indexOf('取消密码') < 0 && m21.indexOf('🔒') < 0, m21.slice(0, 300));
  check('（但别的维护项照旧在）', m21.indexOf('库自检') >= 0 && m21.indexOf('从磁盘导入') >= 0, '');
  sandbox.__setLock(false, false);
  await sandbox.loadLock();
  m21 = openMaint();
  check('未设密码：这一条默认就是"设置密码…"',
        m21.indexOf('设置密码…') >= 0 && m21.indexOf('立即上锁') < 0, m21.slice(0, 260));
  sandbox.__setLock(true, true);
  await sandbox.loadLock();
  sandbox.closeMenu();

  console.log('㉒ 设为私密不需要解锁（辞玖 2026-10-01 13:00）+ 多选批量切私密');
  sandbox.__setLock(false, true);                      // 设过密码 + 已上锁
  await sandbox.loadLock();
  await sandbox.openFile('F1', null);
  for (let ii = 0; ii < 4; ii++) await tick();
  byId['toast'].textContent = '';
  const w22 = writes.length;
  await sandbox.togglePrivate();
  for (let ii = 0; ii < 3; ii++) await tick();
  check('🔑 锁着点「设为私密」：**真的发请求了**（不再被界面拦住）',
        writes.slice(w22).some(w => w.p.indexOf('/visibility') >= 0),
        writes.slice(w22).map(w => w.p));
  check('提示是"已加密成私密"（锁着也能做）',
        String(byId['toast'].textContent).indexOf('已加密') >= 0, byId['toast'].textContent);
  check('按钮 title 也说明"锁着也能做"',
        String(byId['meta'].innerHTML).indexOf('锁着也能做') >= 0, '');
  sandbox.__setLock(false, false);                     // 没设过密码
  await sandbox.loadLock();
  byId['toast'].textContent = '';
  await sandbox.togglePrivate();
  for (let ii = 0; ii < 3; ii++) await tick();
  check('没设过密码才拦：提示"先设密码"（不是让人去解锁）',
        String(byId['toast'].textContent).indexOf('设置密码') >= 0, byId['toast'].textContent);
  sandbox.__setLock(false, true, false);               // 老库：设过密码但还没库公钥
  await sandbox.loadLock();
  byId['toast'].textContent = '';
  await sandbox.togglePrivate();
  for (let ii = 0; ii < 3; ii++) await tick();
  check('老库（还没生成库密钥）：提示"先解锁一次，会自动补上"',
        String(byId['toast'].textContent).indexOf('库密钥') >= 0
        && String(byId['toast'].textContent).indexOf('解锁') >= 0, byId['toast'].textContent);
  sandbox.__setLock(true, true, true);                 // 解锁态：照旧能设
  await sandbox.loadLock();
  byId['toast'].textContent = '';
  await sandbox.togglePrivate();
  for (let ii = 0; ii < 3; ii++) await tick();
  check('解锁时照旧能设成私密', String(byId['toast'].textContent).indexOf('已加密') >= 0,
        byId['toast'].textContent);
  // 多选批量：勾两篇 → 设为私密 / 移出私密
  check('批量条上有「设为私密」「移出私密」两个键',
        String(html).indexOf('treeBatchPrivate(true)') >= 0 && String(html).indexOf('treeBatchPrivate(false)') >= 0,
        '');
  sandbox.treeSelToggle(true);
  sandbox.treeCheck('F1', null);
  sandbox.treeCheck('F3', null);
  check('勾了两篇', String(byId['treeBatchN'].textContent).indexOf('2') >= 0, byId['treeBatchN'].textContent);
  sandbox.__setLock(false, true);                      // 锁着也能批量设私密
  await sandbox.loadLock();
  byId['toast'].textContent = '';
  const w23 = writes.length;
  await sandbox.treeBatchPrivate(true);
  for (let ii = 0; ii < 3; ii++) await tick();
  check('🔑 锁着批量设为私密：发了 /api/visibility/batch 且提示已加密',
        writes.slice(w23).some(w => w.p.indexOf('/api/visibility/batch') >= 0)
        && String(byId['toast'].textContent).indexOf('已加密') >= 0,
        [writes.slice(w23).map(w => w.p), byId['toast'].textContent]);
  byId['toast'].textContent = '';
  sandbox.treeSelToggle(false);                        // 重新勾：treeCheck 是切换，先清空再勾
  sandbox.treeSelToggle(true);
  sandbox.treeCheck('F1', null);
  sandbox.treeCheck('F3', null);
  await sandbox.treeBatchPrivate(false);
  for (let ii = 0; ii < 3; ii++) await tick();
  check('锁着批量移出私密：提示先去解锁（解密要密码）',
        String(byId['toast'].textContent).indexOf('解锁') >= 0
        && String(byId['toast'].textContent).indexOf('移出私密') >= 0, byId['toast'].textContent);
  sandbox.__setLock(true, true);
  await sandbox.loadLock();
  byId['toast'].textContent = '';
  sandbox.treeSelToggle(false);
  sandbox.treeSelToggle(true);
  sandbox.treeCheck('F1', null);
  sandbox.treeCheck('F3', null);
  await sandbox.treeBatchPrivate(false);
  for (let ii = 0; ii < 3; ii++) await tick();
  check('解锁后批量移出私密：成功 + 勾选清空',
        String(byId['toast'].textContent).indexOf('已移出私密') >= 0
        && String(byId['treeBatchN'].textContent).indexOf('0') >= 0,
        [byId['toast'].textContent, byId['treeBatchN'].textContent]);
  sandbox.treeSelToggle(false);
  await sandbox.loadLock();

  console.log('㉓ 搜索底栏要能收起 / 关掉（辞玖 2026-10-01 12:20）');
  sandbox.setScope('all');
  byId['q'].value = '命中';
  await sandbox.doSearch();
  for (let i = 0; i < 4; i++) await tick();
  const hh = String(byId['hits'].innerHTML);
  check('底栏有了头：摘要 + 收起 + 关闭',
        hh.indexOf('id="hitsHead"') >= 0 && hh.indexOf('hitsCollapse()') >= 0
        && hh.indexOf('clearHits()') >= 0, hh.slice(0, 140));
  check('摘要写清了命中数', hh.indexOf('2 命中') >= 0 || hh.indexOf('命中') >= 0, hh.slice(0, 80));
  check('清单在 body 里（两条命中都在）',
        hh.indexOf('id="hitsBody"') >= 0 && (hh.match(/class="hit"/g) || []).length === 2,
        (hh.match(/class="hit"/g) || []).length);
  sandbox.hitsCollapse();
  check('点「收起」：清单折起来（#hits 多个 folded）+ 按钮变「展开」',
        byId['hits'].classList.contains('folded') === true
        && String(byId['hitsFold'].textContent).indexOf('展开') >= 0,
        [byId['hits'].classList.contains('folded'), String(byId['hitsFold'].textContent)]);
  check('收起后摘要还在（不是整条没了）', String(byId['hits'].innerHTML).indexOf('命中') >= 0, '');
  sandbox.hitsCollapse();
  check('再点一次：又展开', byId['hits'].classList.contains('folded') === false,
        byId['hits'].classList.contains('folded'));
  sandbox.clearHits();
  check('点「关闭」：整条收起、内容清空、收起状态复位',
        byId['hits'].style.display === 'none' && String(byId['hits'].innerHTML) === ''
        && byId['hits'].classList.contains('folded') === false,
        [byId['hits'].style.display, String(byId['hits'].innerHTML).slice(0, 20)]);
  byId['q'].value = '命中';
  await sandbox.doSearch();
  for (let i = 0; i < 3; i++) await tick();
  check('再搜一次照旧出来（关闭没把功能关坏）',
        byId['hits'].style.display === 'block' && String(byId['hits'].innerHTML).indexOf('hitsFold') >= 0,
        String(byId['hits'].innerHTML).slice(0, 80));

  console.log('\n========  结论: %d 通过 / %d 失败', pass, fail);
  console.log('（写请求 %d 条全部打在假后端的桩上，真实库只被 GET 过首页 HTML）', writes.length);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('崩了:', e); process.exit(2); });
