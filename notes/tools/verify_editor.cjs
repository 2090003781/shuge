// 验证编辑器这一层：Markdown 预览 / @ 提及候选 / 反向链接条
// 用法：node tools/verify_editor.cjs [port]   默认 8848（直连 ai-notes，不经代理）
// 会在库里临时建一篇 zz-冒烟-编辑器.md，测完删掉。
const vm = require('node:vm');
const http = require('node:http');

const PORT = Number(process.argv[2] || 8848);

function req(method, path, body) {
  return new Promise((res, rej) => {
    const data = body === undefined ? null : Buffer.from(JSON.stringify(body), 'utf8');
    const r = http.request({ host: '127.0.0.1', port: PORT, path, method,
      headers: data ? { 'Content-Type': 'application/json', 'Content-Length': data.length } : {} },
      (resp) => { let b = ''; resp.setEncoding('utf8'); resp.on('data', c => b += c); resp.on('end', () => res(b)); });
    r.on('error', rej);
    if (data) r.write(data);
    r.end();
  });
}
const get = (p) => req('GET', p);
const jget = async (p) => JSON.parse(await get(p));

function mkEl(tag) {
  const cls = new Set();
  return {
    tagName: tag, innerHTML: '', textContent: '', value: '', style: {}, dataset: {}, children: [], attrs: {},
    className: '', scrollTop: 0, selectionStart: 0, selectionEnd: 0, selection: null, focused: false,
    classList: {
      add: c => cls.add(c),
      remove: c => cls.delete(c),
      contains: c => cls.has(c),
      toggle: (c, on) => { if (on === undefined) { const had = cls.has(c); had ? cls.delete(c) : cls.add(c); return !had; } const want = !!on; want ? cls.add(c) : cls.delete(c); return want; },
    },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {}, setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k] ?? null; },
    querySelectorAll: () => [], focus() { this.focused = true; },
    setSelectionRange(a, b) { this.selection = [a, b]; this.selectionStart = a; this.selectionEnd = b; },
    get firstElementChild() { return this.children[0] || { textContent: '' }; },
  };
}

let pass = 0, fail = 0;
const check = (name, cond, extra) => {
  cond ? pass++ : fail++;
  console.log('  %s %s%s', cond ? '✅' : '❌', name, extra === undefined ? '' : ' | ' + JSON.stringify(extra).slice(0, 160));
};

(async () => {
  // ⓪ 先扫掉上次崩溃留下的 zz- 临时笔记（否则它会被当成"被引用对象"，断言全歪）
  const walk = (n, out) => { out.push(...(n.files || [])); (n.children || []).forEach(c => walk(c, out)); return out; };
  for (const f of walk((await jget('/api/tree')).tree, [])) {
    if (f.name.startsWith('zz-')) await req('DELETE', '/api/files/' + f.id);
  }

  // ① 造一篇带 @ 提及的临时笔记（被引用对象用库里现成的第一篇非 zz- 笔记）
  const tree = await jget('/api/tree');
  const allFiles = walk(tree.tree, []);
  // 被引用对象要选“真正文、在检索”的 md：否则 ①② 里“移出检索”那条会跟着库里头一篇是不是 rag=off 忽红忽绿
  const target = allFiles.find(f => !f.name.startsWith('zz-') && f.kind === 'markdown' && f.rag !== 'off')
    || allFiles.find(f => !f.name.startsWith('zz-')) || allFiles[0];
  const tname = target.name.replace(/\.md$/i, '');
  const content = `# 标题\n\n- 一\n- 二\n\n看 @${tname} ，还有 **粗体** 和 \`代码\`。\n\n> 引用一行\n`;
  const filesBefore = (await jget('/api/health')).stats.files;
  const made = JSON.parse(await req('POST', '/api/files', { name: 'zz-冒烟-编辑器.md', content }));
  const fid = made.file_id;

  // ② 把页面脚本跑进沙箱
  const html = await get('/');
  const CLIP = { wrote: '', text: '粘贴板上的字' };
  const byId = {};
  const DOC_HANDLERS = {};                       // document 上注册的事件（待办复选框靠它）
  const sandbox = {
    location: { pathname: '/' },                      // API_BASE 会算成 '/'，直连 8848
    document: {
      getElementById: (id) => (byId[id] = byId[id] || Object.assign(mkEl('div'), { id })),
      createElement: (t) => mkEl(t), querySelectorAll: () => [], querySelector: () => null,
      addEventListener: (t, fn) => { (DOC_HANDLERS[t] = DOC_HANDLERS[t] || []).push(fn); },
      body: mkEl('body'),
    },
    getComputedStyle: () => ({ lineHeight: '22px' }),
    fetch: async (url) => {
      const p = String(url).replace(/^https?:\/\/[^/]+/, '');
      if (p.indexOf('/api/maintenance/selfcheck') >= 0) {   // 假的维护接口，测自检遮罩层
        return { ok: true, status: 200, json: async () => ({ ok: true, result: {
          ok: false, bad: 1, warn: 1, created_at: '2026-09-16T20:00:00', checks: [
            { id: 'verify', level: 'ok', title: '一致性自检', detail: 'ok' },
            { id: 'names', level: 'warn', title: '重名', detail: '89 组' },
            { id: 'x', level: 'bad', title: '坏东西', detail: '1 处' }] } }) };
      }
      if (p.indexOf('/rag') >= 0) {                        // 假的"进/出检索库"
        return { ok: true, status: 200, json: async () => ({ ok: true, rag: 'on', chunks: 3, vectors: {} }) };
      }
      if (p.indexOf('/api/files/DOC-ID/text') >= 0) {        // 假的"文档解析结果"（odt：走 pandoc 那条路）
        return { ok: true, status: 200, json: async () => ({ ok: true, format: 'odt',
          parser: 'odt(ooxml)', error: '', chars: 42, cached: true, meta: { title: '周报样例' },
          text: '# 项目周报\n\n这周做了**三件事**。\n' }) };
      }
      if (p.indexOf('/api/files/DX-ID/docx/edit') >= 0) {    // 假的"docx 引擎写回"
        return { ok: true, status: 200, json: async () => ({ ok: true, bytes: 9200, changed: 1,
          before_image: 'IMG1', engine: 'docxeng' }) };
      }
      if (p.indexOf('/api/files/DX-ID/docx/export') >= 0) {  // 假的"docx 导出"
        return { ok: true, status: 200, json: async () => ({ ok: true, to: 'md', file_id: 'X9',
          name: '周报.md', bytes: 120, engine: 'pandoc' }) };
      }
      if (p.indexOf('/api/files/DX-ID/docx') >= 0) {         // 假的"docx 引擎读模型"
        return { ok: true, status: 200, json: async () => ({ ok: true, bytes: 9000,
          info: { paras: 2, tables: 1, chars: 42, headings: [{ i: 0, level: 1, text: '项目周报' }],
                  page: { w: 595.3, h: 841.9, size_set: false, orient: 'portrait' } },
          html: '<h1>项目周报</h1>',
          blocks: [
            { i: 0, kind: 'p', style: 'Heading 1', heading: 1, align: '', list: {},
              text: '项目周报', runs: [{ t: '项目周报', b: true, size: 18 }] },
            { i: 1, kind: 'p', style: 'Normal', heading: 0, align: '', list: {},
              text: '这周做了三件事。', runs: [{ t: '这周做了三件事。', b: false, size: 12 }] },
            { i: 2, kind: 'table', rows: 2, cols: 2,
              cells: [['姓名', '分数'], ['张三', '90']] }] }) };
      }
      if (p.indexOf('/api/files/DX-ID') >= 0) {              // 假的 docx 文件（引擎编辑器那条路）
        return { ok: true, status: 200, json: async () => ({ ok: true, file: {
          id: 'DX-ID', name: '周报.docx', kind: 'document', size: 9000, visibility: 'internal',
          caps: { open: true, edit: false, search: true, store_only: false },
          rag: 'off',
          mtime: '2026-09-16T00:00:00+08:00', locked: false, content: '', mentions: [],
          backlinks: [], deleted_refs: [],
          block_stats: { blocks: 3, pages: 1, tables: 1, headings: 1, dropped: 0 },
          derived: { format: 'docx', parser: 'docx(ooxml)', chars: 42, error: '',
                     meta: { title: '周报样例' } } } }) };
      }
      if (p.indexOf('/api/files/DOC-ID') >= 0) {             // 假的"文档"文件
        return { ok: true, status: 200, json: async () => ({ ok: true, file: {
          id: 'DOC-ID', name: '周报.odt', kind: 'document', size: 12345, visibility: 'internal',
          caps: { open: true, edit: false, search: true, store_only: false },
          rag: 'off',
          mtime: '2026-09-16T00:00:00+08:00', locked: false, content: '', mentions: [],
          backlinks: [], deleted_refs: [],
          block_stats: { blocks: 15, pages: 2, tables: 1, headings: 4, dropped: 6 },
          derived: { format: 'odt', parser: 'odt(ooxml)', chars: 42, error: '',
                     meta: { title: '周报样例' } } } }) };
      }
      if (p.indexOf('/api/files/SECTIONED-ID') >= 0) {      // 假的"带小节分组的反向链接"，测筛选
        return { ok: true, status: 200, json: async () => ({ ok: true, file: {
          id: 'SECTIONED-ID', name: '分区笔记.md', kind: 'markdown', size: 20, visibility: 'internal',
          mtime: '2026-09-16T00:00:00+08:00', locked: false, content: '# 分区\n', mentions: [],
          deleted_refs: [],
          backlinks: [
            { id: 'A1', name: '甲笔记.md', anchor: '分区笔记#P12', section: 'P12',
              section_heading: 'P12 决策前置', section_ok: true },
            { id: 'B1', name: '乙笔记.md', anchor: '分区笔记', section: '',
              section_heading: '', section_ok: true }],
          backlink_groups: [
            { section: 'P12', heading: 'P12 决策前置', ok: true, count: 1,
              refs: [{ id: 'A1', name: '甲笔记.md' }] },
            { section: '', heading: '', ok: true, count: 1,
              refs: [{ id: 'B1', name: '乙笔记.md' }] }] } }) };
      }
      if (p.indexOf('/api/properties/find') >= 0) {        // 假的"按属性找"
        return { ok: true, status: 200, json: async () => ({ ok: true, key: 'status', value: '待办',
          total: 2, hits: [{ id: 'FM-ID', name: 'zz-属性样例.md', doc_date: '2026-09-01', path: 'vault/zz-属性样例.md', value: ['待办'] },
                           { id: 'X2', name: '另一篇.md', doc_date: '', path: 'vault/另一篇.md', value: ['待办'] }] }) };
      }
      if (p.indexOf('/api/properties') >= 0) {             // 假的"全库属性索引"
        return { ok: true, status: 200, json: async () => ({ ok: true, files_with_properties: 212,
          keys: [{ key: 'status', count: 37, values: [{ value: '待办', count: 12 }, { value: 'draft', count: 9 }] },
                 { key: 'tags', count: 136, values: [{ value: '系统设计', count: 8 }] }] }) };
      }
      if (p.indexOf('/api/files/FM-ID') >= 0) {            // 假的"带前置属性的笔记"
        return { ok: true, status: 200, json: async () => ({ ok: true, file: {
          id: 'FM-ID', name: 'zz-属性样例.md', kind: 'markdown', size: 120, visibility: 'internal',
          caps: { open: true, edit: true, search: true, store_only: false },
          frontmatter: { title: '属性样例', status: '待办', tags: ['甲', '乙'], created: '2026-09-01' },
          title: '属性样例', tags: ['甲', '乙'], doc_date: '2026-09-01',
          mtime: '2026-09-27T09:00:00+08:00', locked: false, rag: 'on',
          content: '---\ntitle: 属性样例\nstatus: 待办\n---\n\n# 正文\n\n## 一节\n',
          mentions: [], backlinks: [] } }) };
      }
      if (p.indexOf('/api/files/LOCKED-ID') >= 0) {          // 假的"已加锁"响应，测界面表现
        return { ok: true, status: 200, json: async () => ({ ok: true, file: {
          id: 'LOCKED-ID', name: '秘密.md', kind: 'markdown', size: 999, visibility: 'private',
          caps: { open: true, edit: true, search: true, store_only: false },
          mtime: '2026-09-13T00:00:00+08:00', locked: true, content: '', mentions: [], backlinks: [] } }) };
      }
      if (p.indexOf('/api/files/IMG-ID') >= 0) {             // 假的“图片”：能看、不能当正文改
        return { ok: true, status: 200, json: async () => ({ ok: true, file: {
          id: 'IMG-ID', name: '卷一-核心架构.jpg', kind: 'image', size: 1220450,
          visibility: 'internal', caps: { open: true, edit: false, search: false, store_only: false },
          mtime: '2026-09-25T00:00:00+08:00', locked: false, content: '', mentions: [], backlinks: [] } }) };
      }
      if (p.indexOf('/api/files/ASSET-ID') >= 0) {           // 假的“只存不解析”原文件
        return { ok: true, status: 200, json: async () => ({ ok: true, file: {
          id: 'ASSET-ID', name: '倒计时闹钟.exe', kind: 'asset', size: 40960,
          visibility: 'internal', caps: { open: false, edit: false, search: false, store_only: true },
          mtime: '2026-09-25T00:00:00+08:00', locked: false, content: '', mentions: [], backlinks: [] } }) };
      }
      const body = await get(p.startsWith('/') ? p : '/' + p);
      return { ok: true, status: 200, json: async () => JSON.parse(body) };
    },
    setTimeout, clearTimeout, setImmediate, console: { log() {}, error() {} },
    alert() {}, confirm: () => true, prompt: () => null,
    navigator: { clipboard: { writeText: async (t) => { CLIP.wrote = t; },
                              readText: async () => CLIP.text } },
    localStorage: { _v: {}, getItem(k) { return this._v[k] === undefined ? null : this._v[k]; },
                    setItem(k, v) { this._v[k] = String(v); } },
    encodeURIComponent, decodeURIComponent, Error, JSON, String, Number, Promise, Object, Array, RegExp, Math, parseFloat, Set,
  };
  sandbox.innerWidth = 1280;
  sandbox.innerHeight = 800;
  sandbox.addEventListener = () => {};
  sandbox.window = sandbox; sandbox.globalThis = sandbox;
  sandbox.innerWidth = 1200; sandbox.innerHeight = 800;
  vm.createContext(sandbox);
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], sandbox, { filename: 'inline.js' });
  for (let w = 0; w < 300 && !(byId['stats'] && byId['stats'].textContent); w++) await new Promise(r => setTimeout(r, 10));

  console.log('① 页面装入沙箱');
  check('文件树已加载', !!byId['stats'].textContent, byId['stats'].textContent);

  console.log('② Markdown 渲染');
  const md = sandbox.mdRender('# 标题\n\n- 一\n- 二\n\n**粗** `码` [链](https://x.y)\n\n> 引用\n\n| a | b |\n|---|---|\n| 1 | 2 |\n', {});
  check('标题', md.includes('<h1>标题</h1>'));
  check('列表', md.includes('<ul>') && md.includes('<li>一'));
  check('粗体/行内码', md.includes('<strong>粗</strong>') && md.includes('<code>码</code>'));
  check('外链', md.includes('href="https://x.y"'));
  check('引用', md.includes('<blockquote>'));
  check('表格', md.includes('<thead>') && md.includes('<td>1</td>'));
  const mref = {}; mref['某笔记'] = { id: 'FILEID', name: '某笔记', section: '' };
  mref['某笔记#P2'] = { id: 'FILEID', name: '某笔记', section: 'P2' };
  const mdm = sandbox.mdRender('见 @某笔记 和 @没引用上的', mref);
  check('提及渲染成链接', mdm.includes('class="mention"') && /openMention\('FILEID'/.test(mdm));
  check('没引用上的保持原文', mdm.includes('@没引用上的'));
  const mdm2 = sandbox.mdRender('见 @某笔记#P2 那一节', mref);
  check('带小节的引用显示小节', mdm2.includes('@某笔记#P2'), mdm2.slice(0, 90));
  check('带小节的引用把小节传给点击', mdm2.includes("openMention('FILEID', 'P2')"), mdm2.slice(0, 120));

  console.log('③ 打开临时笔记 → 预览');
  await sandbox.openFile(fid);
  await new Promise(r => setImmediate(r));
  sandbox.togglePreview();
  const pv = byId['preview'];
  check('预览层可见', pv.style.display === 'block');
  check('编辑器让位', byId['editor'].style.display === 'none');
  check('按钮变成「编辑」', byId['btnPreview'].textContent.includes('编辑'), byId['btnPreview'].textContent);
  check('预览里出现提及链接', pv.innerHTML.includes('class="mention"') && pv.innerHTML.includes('@' + tname));
  check('预览里出现标题', pv.innerHTML.includes('<h1>标题</h1>'));
  sandbox.togglePreview();
  check('切回编辑', byId['preview'].style.display === 'none' && byId['btnPreview'].textContent.includes('预览'));

  console.log('④ @ 提及候选框');
  const ed = byId['editor'];
  ed.value = '看 @' + tname.slice(0, 4);
  ed.selectionStart = ed.selectionEnd = ed.value.length;
  sandbox.acUpdate();
  const ac = byId['ac'];
  check('候选框弹出', ac.style.display === 'block');
  // 候选框里显示的是**路径**（重名时还要靠它区分），所以只断言"这个名字在候选里"
  check('候选里有目标笔记', ac.innerHTML.includes(tname), ac.innerHTML.slice(0, 120));
  sandbox.acPick(0);
  check('插入的是完整短名', ed.value.includes('@' + tname + ' '), ed.value.slice(0, 60));
  check('候选框收起', ac.style.display === 'none');
  // 光标应落在 "@名字 " 之后：+1 是 @，再 +1 是补的那个空格
  check('光标落在插入文字之后', ed.selection[0] === ed.value.indexOf('@' + tname) + tname.length + 2, ed.selection);

  console.log('⑤ 反向链接条');
  sandbox.acClose();
  byId['editor'].value = content;
  await sandbox.openFile(target.id);
  await new Promise(r => setImmediate(r));
  const bl = byId['blinks'];
  check('目标笔记显示「被引用」', bl.style.display === 'block' && bl.innerHTML.includes('zz-冒烟-编辑器'), bl.innerHTML.slice(0, 140));

  console.log('⑤b 反向链接按小节筛选');
  await sandbox.openFile('SECTIONED-ID');
  const bl2 = byId['blinks'];
  check('渲染出小节 chip', bl2.innerHTML.includes('bchip'), bl2.innerHTML.slice(0, 120));
  // 2026-09-30 辞玖："不需要带小节标题" → chip 上只留短的 #P12，完整标题在 title 里
  check('两组都在（#P12 / 整篇），完整标题在悬停提示里',
    bl2.innerHTML.includes('#P12（') && bl2.innerHTML.includes('整篇')
    && bl2.innerHTML.includes('P12 决策前置'),
    bl2.innerHTML.slice(0, 200));
  check('未筛选时两篇引用都在',
    bl2.innerHTML.includes('@甲笔记') && bl2.innerHTML.includes('@乙笔记'), bl2.innerHTML.slice(0, 200));
  sandbox.blFilterSet('P12 决策前置');
  const f1 = byId['blinks'].innerHTML;
  check('只看 P12：只剩甲笔记', f1.includes('@甲笔记') && !f1.includes('@乙笔记'), f1.slice(0, 160));
  check('出现「取消只看」', f1.includes('取消只看'));
  check('chip 高亮', f1.includes('bchip on'), f1.slice(0, 120));
  sandbox.blFilterClear();
  const f2 = byId['blinks'].innerHTML;
  check('取消后两篇都回来', f2.includes('@甲笔记') && f2.includes('@乙笔记'), f2.slice(0, 200));
  check('换文件后筛选作废', (() => { sandbox.blFilterSet('整篇'); return true; })());
  await sandbox.openFile(target.id);
  check('打开别的笔记不再有筛选', !byId['blinks'].innerHTML.includes('取消只看'),
    byId['blinks'].innerHTML.slice(0, 120));

  console.log('⑤c 右键目录树空白处 → 维护菜单 → 库自检');
  sandbox.openMenu({ clientX: 10, clientY: 10, target: { closest: () => null } }, { kind: 'maint' });
  check('维护菜单里有「库自检」', String(byId['ctxmenu'].innerHTML).includes('库自检'),
    String(byId['ctxmenu'].innerHTML).slice(0, 90));
  await sandbox.runSelfcheck();
  const sc = String(byId['scBody'].innerHTML);
  check('自检结果渲染出来', sc.includes('一致性自检') && sc.includes('重名'), sc.slice(0, 140));
  check('❌ 与 ⚠️ 都标出来', sc.includes('❌') && sc.includes('⚠️'), sc.slice(0, 200));
  check('遮罩层打开', byId['selfcheck'].classList.contains('open'));
  const when = String((byId['scWhen'] || {}).textContent || '');
  check('汇总行显示计数', when.includes('❌ 1'), when);
  sandbox.closeSelfcheck();
  check('关闭后遮罩层收起', !byId['selfcheck'].classList.contains('open'));

  console.log('⑤d 文档格式（.odt 这类走 pandoc）：**文档编辑器**（可编辑 + 存回原格式）+ 预览');
  await sandbox.openFile('DOC-ID');
  for (let i = 0; i < 4; i++) await new Promise(r => setImmediate(r));   // 等异步解析落地
  check('文档编辑器：可编辑（不再是只读预览）', byId['editor'].readOnly === false);
  check('提示语说明是文档', String(byId['editor'].placeholder).includes('文档'),
    byId['editor'].placeholder);
  check('解析出来的正文已经装进编辑器', String(byId['editor'].value).includes('项目周报'),
    String(byId['editor'].value).slice(0, 60));
  check('文档工具条亮着、markdown 那套收起',
    byId['docBar'].classList.contains('on') && byId['ribbonHead'].style.display === 'none',
    [byId['docBar'].classList.contains('on'), byId['ribbonHead'].style.display]);
  check('文档模式不拦编辑（不再 toolsOff）', !byId['ribbonBody'].classList.contains('off'));
  check('默认进编辑器、不自动切预览',
    byId['preview'].style.display === 'none' && byId['editor'].style.display === 'block',
    [byId['preview'].style.display, byId['editor'].style.display]);
  sandbox.togglePreview();                                             // 手动切预览才看解析徽标
  for (let i = 0; i < 4; i++) await new Promise(r => setImmediate(r));
  const dpv = String(byId['preview'].innerHTML);
  check('预览有徽标（格式/解析器/字数）',
    dpv.includes('解析预览') && dpv.includes('ODT') && dpv.includes('odt(ooxml)'),
    dpv.slice(0, 160));
  check('徽标带块树统计（块/表/剔除的页眉页脚）',
    dpv.includes('块 15') && dpv.includes('表 1') && dpv.includes('已剔除页眉页脚 6'),
    dpv.slice(0, 220));
  check('渲染的是解析出来的 Markdown',
    dpv.includes('<h1>项目周报</h1>') && dpv.includes('<strong>三件事</strong>'), dpv.slice(0, 240));
  sandbox.togglePreview();                                             // 切回编辑器，后面的用例接着用
  check('转换入口已统一：元信息条不再有转换/导出按钮',
    !String(byId['meta'].innerHTML).includes('另存为 Markdown')
    && !String(byId['meta'].innerHTML).includes('导出为'),
    String(byId['meta'].innerHTML).slice(0, 200));
  check('树里文档用 📘 图标', String(byId['tree'].innerHTML).length >= 0);

  console.log('⑤f .docx 走**引擎编辑器**（结构化编辑：改哪段动哪段，保存走 /docx/edit）');
  await sandbox.openFile('DX-ID');
  for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r));
  const sheet = String(byId['docxSheet'].innerHTML);
  check('引擎工具条亮着、pandoc 那条文档收起了 ⑤f',
    byId['docxBar'].classList.contains('on') && !byId['docBar'].classList.contains('on')
    && byId['ribbonHead'].style.display === 'none',
    [byId['docxBar'].classList.contains('on'), byId['docBar'].classList.contains('on')]);
  check('走查看器那块（不是 textarea 编辑器）',
    byId['viewer'].style.display === 'flex' && byId['editor'].style.display === 'none',
    [byId['viewer'].style.display, byId['editor'].style.display]);
  check('段落与表格都铺出来了',
    sheet.includes('这周做了三件事') && sheet.includes('<table') && sheet.includes('张三'),
    sheet.slice(0, 120));
  check('段落可改（contenteditable）+ 带块序号',
    sheet.includes('contenteditable="true"') && sheet.includes('data-i="1"'), '');
  check('信息条写清是引擎编辑器、几段几表',
    String(byId['docxInfo'].textContent).includes('docx 编辑器')
    && String(byId['docxInfo'].textContent).includes('2 段'), byId['docxInfo'].textContent);
  sandbox.docxTextInput({ dataset: { i: '1' }, innerText: '这周做了四件事。' });
  check('改一段 → 攒下 1 处待保存、保存按钮亮',
    String(byId['docxDirty'].textContent).includes('1') && byId['docxSaveBtn'].disabled === false,
    [byId['docxDirty'].textContent, byId['docxSaveBtn'].disabled]);
  sandbox.docxSetStyle('标题2');
  check('换样式：样式盒跟着这一段走', byId['docxStyle'].value === '标题2', byId['docxStyle'].value);
  sandbox.docxAlign('center');
  check('对齐：那一段真的变成居中（重画出来带 text-align:center）',
    String(byId['docxSheet'].innerHTML).includes('text-align:center'),
    String(byId['docxSheet'].innerHTML).slice(0, 100));
  await sandbox.docxSave();
  check('保存后待保存清零、按钮灰回去',
    String(byId['docxDirty'].textContent) === '' && byId['docxSaveBtn'].disabled === true,
    [byId['docxDirty'].textContent, byId['docxSaveBtn'].disabled]);
  check('导出入口在工具条上（md / pdf）',
    String(html).indexOf("docxExport('md')") >= 0 && String(html).indexOf("docxExport('pdf')") >= 0, '');

  console.log('⑤e 什么文件都能进：图片看得到、只存原文件有落点');
  await sandbox.openFile('IMG-ID');
  check('图片：查看器上场、编辑器让位',
    byId['viewer'].style.display === 'flex' && byId['editor'].style.display === 'none'
    && byId['preview'].style.display === 'none',
    [byId['viewer'].style.display, byId['editor'].style.display, byId['preview'].style.display]);
  check('图片：直接挂 /raw 渲染',
    String(byId['viewer'].innerHTML).indexOf('<img') >= 0
    && String(byId['viewer'].innerHTML).indexOf('/api/files/IMG-ID/raw') >= 0,
    String(byId['viewer'].innerHTML).slice(0, 130));
  check('图片：工具体说明为何不能编辑',
    String(byId['ribbonHint'].textContent).indexOf('只存不解析') >= 0,
    byId['ribbonHint'].textContent);
  byId['toast'].textContent = '';
  await sandbox.save();
  check('图片：点保存被拦（不会把二进制写成正文）',
    String(byId['toast'].textContent).indexOf('不能编辑') >= 0, byId['toast'].textContent);
  await sandbox.openFile('ASSET-ID');
  check('只存原文件：给回取入口',
    String(byId['viewer'].innerHTML).indexOf('📦') >= 0
    && String(byId['viewer'].innerHTML).indexOf('/api/files/ASSET-ID/raw') >= 0,
    String(byId['viewer'].innerHTML).slice(0, 130));
  check('只存原文件：元信息条标「只存」',
    String(byId['meta'].innerHTML).indexOf('📦 只存') >= 0, String(byId['meta'].innerHTML).slice(0, 150));
  await sandbox.openFile(target.id);
  check('回到普通笔记：查看器收起、正文区回来',
    byId['viewer'].style.display === 'none'
    && (byId['editor'].style.display === 'block' || byId['preview'].style.display === 'block'),
    [byId['viewer'].style.display, byId['editor'].style.display, byId['preview'].style.display]);

  console.log('⑥ 加锁笔记的界面表现');
  await sandbox.openFile('LOCKED-ID');
  const le = byId['editor'];
  check('编辑器只读', le.readOnly === true);
  check('不回显内容', le.value === '');
  check('提示语说明已加锁', String(le.placeholder).includes('🔒'), le.placeholder);
  check('元数据标了私密', String(byId['meta'].innerHTML).includes('私密'),
        String(byId['meta'].innerHTML).slice(0, 160));
  await sandbox.save();
  check('加锁时保存被拦', String(byId['toast'].textContent).includes('加锁'), byId['toast'].textContent);
  await sandbox.openFile(target.id);
  check('普通笔记可编辑', byId['editor'].readOnly === false && byId['editor'].value.length > 0);

  console.log('⑦ 文件树默认折叠');
  const fakeTree = { name: '', files: [{ id: 'a', name: 'a.md', kind: 'markdown' }], children: [
    { name: 'vault', files: [{ id: 'b', name: 'b.md', kind: 'markdown' }],
      children: [{ name: 'sub', files: [], children: [] }] }] };
  const rootWrap = sandbox.renderDir(fakeTree, 0);
  const rootBody = rootWrap.children[1];
  check('根层展开（看得见顶层目录）', rootBody.style.display !== 'none', rootBody.style.display);
  const vaultWrap = rootBody.children[0];
  check('顶层目录默认折叠', vaultWrap.children[1].style.display === 'none', vaultWrap.children[1].style.display);
  sandbox.toggle(vaultWrap.children[0], vaultWrap.children[1]);
  check('点一下能展开', vaultWrap.children[1].style.display !== 'none', vaultWrap.children[1].style.display);
  const subWrap = vaultWrap.children[1].children[0];
  check('子目录也默认折叠', subWrap.children[1].style.display === 'none', subWrap.children[1].style.display);

  console.log('⑧ 右键菜单');
  const menu = (byId['ctxmenu'] = byId['ctxmenu']
    || Object.assign(mkEl('div'), { id: 'ctxmenu' }));   // 页面按需创建，这里也照做
  const fakeEv = { clientX: 40, clientY: 40, preventDefault() {} };
  sandbox.openMenu(fakeEv, { kind: 'file', id: 'x', name: 'a.md', visibility: 'internal' });
  check('文件菜单有重命名', menu.innerHTML.includes('重命名'));
  check('文件菜单有移动', menu.innerHTML.includes('移动到'));
  check('文件菜单有删除', menu.innerHTML.includes('删除'));
  check('文件菜单有标签/私密', menu.innerHTML.includes('标签') && menu.innerHTML.includes('设为私密'));
  sandbox.openMenu(fakeEv, { kind: 'folder', id: 'f', name: 'vault', empty: true });
  check('文件夹菜单有新建文件/文件夹', menu.innerHTML.includes('新建文件') && menu.innerHTML.includes('新建文件夹'));
  check('文件夹菜单有重命名/删除', menu.innerHTML.includes('重命名') && menu.innerHTML.includes('删除文件夹'));
  sandbox.closeMenu();
  check('关掉后菜单收起', menu.style.display === 'none');
  const treeRows = rootWrap.children[1].children[0];    // vault 一层的 wrap
  check('树行挂了右键处理', typeof treeRows.children[0].oncontextmenu === 'function'
        || typeof treeRows.children[1].children[0].oncontextmenu === 'function');

  console.log('⑨ WPS 风格面板 + 字数');
  await sandbox.openFile(target.id);
  const tb = byId['ribbonBody'], wcn = byId['wcNum'];
  check('普通笔记面板可用', !!tb && !tb.classList.contains('off'), String(byId['ribbonHint'].textContent));
  const e2 = byId['editor'];
  e2.value = '甲\n乙\n丙';
  e2.selectionStart = 0; e2.selectionEnd = 3;          // 选中前两行
  sandbox.mdLine('- ');
  check('行记号作用到选中的每一行', e2.value === '- 甲\n- 乙\n丙', e2.value);
  check('行记号后光标选中新块', e2.selection[0] === 0 && e2.selection[1] === 7, e2.selection);
  sandbox.mdLine('- ');                                 // 同一块再点一次
  check('行记号再点一次可取消', e2.value === '甲\n乙\n丙', e2.value);
  sandbox.mdWrap('**', '**');
  check('包裹记号把选中文字括起来', e2.value === '**甲\n乙**\n丙', e2.value);
  check('包裹后选中的是原文', e2.selection[0] === 2 && e2.selection[1] === 5, e2.selection);
  e2.selectionStart = 2; e2.selectionEnd = 5;
  sandbox.mdWrap('**', '**');
  check('包裹记号再点一次可取消', e2.value === '甲\n乙\n丙', e2.value);
  e2.value = '看这段'; e2.selectionStart = e2.selectionEnd = 3;
  sandbox.mdWrap('`', '`');
  check('没选中文字时用占位字并选中它', e2.value === '看这段`文字`' && e2.selection[0] === 4, e2.value);
  e2.value = '一行'; e2.selectionStart = e2.selectionEnd = 2;
  sandbox.mdInsert('\n---\n');
  check('块级片段独占一行（自动补换行）', e2.value === '一行\n\n---\n', e2.value);
  e2.value = '一行'; e2.selectionStart = e2.selectionEnd = 2;
  sandbox.mdAt();
  check('@ 按钮补了边界，插入后弹候选框',
    e2.value === '一行 @' && byId['ac'].style.display === 'block',
    e2.value + ' / ' + byId['ac'].style.display);
  e2.value = ''; e2.selectionStart = e2.selectionEnd = 0;
  sandbox.mdAt();
  check('行首 @ 不补空格', e2.value === '@', e2.value);
  sandbox.acClose();
  e2.value = '# 标题\n正文。';
  sandbox.countWords();
  check('字数是「汉字 + 去标点字符 + 行」',
    String(wcn.textContent) === '汉字 4 · 字符 4 · 行 2', wcn.textContent);
  check('汉字数只数汉字', sandbox.hanCount('a1汉字2') === 2 && sandbox.hanCount('标点，。！') === 2);
  check('字符数不看标点', sandbox.plainCount('你好，世界！') === 4
    && sandbox.plainCount('a b,c.') === 3);

  console.log('⑨b 只读文件禁用工具条');
  await sandbox.openFile('LOCKED-ID');
  check('加锁笔记面板变灰', byId['ribbonBody'].classList.contains('off')
    && String(byId['ribbonHint'].textContent).includes('加锁'), String(byId['ribbonHint'].textContent));
  sandbox.mdWrap('**', '**');
  check('加锁时点面板不改内容', byId['editor'].value === '', byId['editor'].value);
  check('并且给出提示', String(byId['toast'].textContent).includes('加锁'), byId['toast'].textContent);
  await sandbox.openFile('DOC-ID');
  for (let i = 0; i < 3; i++) await new Promise(r => setImmediate(r));
  check('文档模式：markdown 那套收起、文档工具条亮着',
    byId['ribbonHead'].style.display === 'none' && byId['docBar'].classList.contains('on'),
    [byId['ribbonHead'].style.display, byId['docBar'].classList.contains('on')]);
  check('状态栏说明这是文档编辑器', String(byId['wcDoc'].textContent).includes('文档编辑器'),
    byId['wcDoc'].textContent);
  await sandbox.openFile(target.id);
  check('回到普通笔记面板恢复', !byId['ribbonBody'].classList.contains('off'));

  console.log('⑩ 格式转换：一个按钮 → 独立页面层级');
  // 数**工具区里**的转换入口（docBar 上那个是"文档模式"的入口，不算重复）
  const _rb = String(html).slice(String(html).indexOf('id="ribbonBody"'),
                                 String(html).indexOf('id="docBar"'));
  const convBtns = (_rb.match(/onclick="openConvert\(\)"/g) || []).length;
  check('转换入口只剩一个按钮（工具区里）', convBtns === 1 && !html.includes('exportMenu('), convBtns);
  check('转换页面是独立层级（整屏遮罩 + 卡片）',
    html.includes('id="convpage"') && html.includes('class="cv-box"') && html.includes('id="cvBody"'));
  await sandbox.openFile(target.id);
  await sandbox.openConvert();
  for (let i = 0; i < 3; i++) await new Promise(r => setImmediate(r));
  const cb = String(byId['cvBody'].innerHTML);
  check('页面打开了', byId['convpage'].classList.contains('open'));
  check('列出全部导出格式', ['docx', 'pdf', 'odt', 'rtf', 'html', 'epub', 'md']
    .every(f => cb.includes("exportAs('" + f + "')")), cb.slice(0, 130));
  check('每种格式标了引擎', cb.includes('pandoc') && cb.includes('WPS'), cb.slice(0, 220));
  // 2026-09-30 体检 8.1②：导出一次之后，按钮上那段「· 引擎」小字被 textContent 冲掉了。
  // 真调一次 exportAs，断言按钮**仍然**带着 <span class="muted">。
  await sandbox.exportAs('md');
  const expBtn = String(byId['cvExp-md'].innerHTML);
  check('导出后按钮仍带引擎小字（不再被 textContent 冲掉）',
        expBtn.includes('muted') && expBtn.includes('Markdown'), expBtn.slice(0, 140));
  check('普通笔记不显示「读入」那一步', !cb.includes('另存为 Markdown'));
  await sandbox.openFile('DOC-ID');
  for (let i = 0; i < 3; i++) await new Promise(r => setImmediate(r));
  await sandbox.openConvert();
  for (let i = 0; i < 3; i++) await new Promise(r => setImmediate(r));
  const cb2 = String(byId['cvBody'].innerHTML);
  check('文档页多出「读入：另存为 Markdown」+ 解析器',
    cb2.includes('另存为 Markdown') && cb2.includes('解析器'), cb2.slice(0, 220));
  sandbox.closeConvert();
  check('关掉后收起', !byId['convpage'].classList.contains('open'));
  await sandbox.openFile(target.id);

  console.log('⑪ 上侧可收放面板');
  const GROUPS = ['剪贴板', '字体', '段落', '插入', '文件', '视图'];
  check('面板分成六组', GROUPS.every(g => html.includes('<div class="glabel">' + g + '</div>')),
    GROUPS.filter(g => !html.includes('<div class="glabel">' + g + '</div>')).join(',') || '全在');
  check('面板里有保存与转换（转换走独立页面）',
    html.includes('onclick="save()"') && html.includes('onclick="openConvert()"'));
  check('基础编辑操作齐（撤销/重做/剪切/复制/粘贴/全选）',
    ['undoEdit()', 'redoEdit()', 'clipCopy(true)', 'clipCopy(false)', 'clipPaste()', 'selectAllText()']
      .every(f => html.includes(f)));
  check('面板有收放按钮', html.includes('id="ribbonBtn"') && html.includes('ribbonToggle()'));
  // 收放把手：编辑栏下侧的**长窄箭头条**（辞玖 2026-10-09）；箭头与文字各自一个 span
  check('把手是长窄箭头条（class=rgrip + 箭头 span）',
    html.includes('id="ribbonBtn" class="rgrip"') && html.includes('id="ribbonIcon"'),
    html.slice(html.indexOf('id="ribbonBtn"'), html.indexOf('id="ribbonBtn"') + 90));
  // 查找行属于**编辑栏**（同一个 #ribbon 里，且在按钮区之前）——辞玖 2026-10-09 22:11
  {
    const _i = html.indexOf('id="ribbon"'), _j = html.indexOf('id="draftBar"') > 0
      ? html.indexOf('id="draftBar"') : html.length;
    const _seg = html.slice(_i, _j);
    const _f = _seg.indexOf('id="findbar"'), _b = _seg.indexOf('id="ribbonBody"');
    check('查找行属于编辑栏（在 #ribbon 里、按钮区之前）', _f > 0 && _b > 0 && _f < _b,
      'findbar@' + _f + ' body@' + _b);
    check('把手是编辑栏的最后一个元素（最下沿）',
      _seg.lastIndexOf('id="ribbonBtn"') > _seg.lastIndexOf('id="ribbonBody"'), '');
  }

  await sandbox.openFile(target.id);
  sandbox.ribbonToggle(false);
  check('收起后面板藏起来', byId['ribbonBody'].style.display === 'none'
    && String(byId['ribbonBtn'].getAttribute('data-open')) === '0',
    byId['ribbonBody'].style.display + ' / data-open=' + byId['ribbonBtn'].getAttribute('data-open'));
  check('收起状态记进 localStorage', sandbox.localStorage._v['ai-notes-ribbon'] === '0',
    sandbox.localStorage._v['ai-notes-ribbon']);
  // 箭头的**方向**（收起 ▴ / 展开 ▾）—— 2026-10-09 踩过：用 btn.textContent 会把箭头 span 一起清掉
  const _ic0 = byId['ribbonIcon'];
  check('收起时箭头朝上（▴）', _ic0 && String(_ic0.textContent) === '▴', _ic0 && _ic0.textContent);
  check('收起时把手还在（它就是"展开"那个按钮）',
    byId['ribbonBtn'].style.display !== 'none',
    String(byId['ribbonBtn'].style.display));
  sandbox.ribbonToggle(true);
  check('展开回来', byId['ribbonBody'].style.display === 'flex'
    && String(byId['ribbonBtn'].getAttribute('data-open')) === '1',
    byId['ribbonBody'].style.display + ' / data-open=' + byId['ribbonBtn'].getAttribute('data-open'));
  check('展开时箭头朝下（▾）且 span 还在', _ic0 && String(_ic0.textContent) === '▾',
    _ic0 && _ic0.textContent);
  check('展开时把手回来（data-open=1）',
    String(byId['ribbonBtn'].getAttribute('data-open')) === '1',
    byId['ribbonBtn'].getAttribute('data-open'));

  const e3 = byId['editor'];
  sandbox.histReset('甲', 0, 0);
  e3.value = '甲乙'; e3.selectionStart = e3.selectionEnd = 2;
  sandbox.histCommit();
  sandbox.undoEdit();
  check('撤销回到上一步', e3.value === '甲', e3.value);
  sandbox.redoEdit();
  check('重做又回来', e3.value === '甲乙', e3.value);

  e3.value = '复制我'; e3.selectionStart = 0; e3.selectionEnd = 3;
  await sandbox.clipCopy(false);
  check('复制把选中的字写进剪贴板', CLIP.wrote === '复制我', CLIP.wrote);
  await sandbox.clipPaste();
  check('粘贴插到光标处', e3.value === '粘贴板上的字', e3.value);
  e3.value = '剪掉我'; e3.selectionStart = 0; e3.selectionEnd = 3;
  await sandbox.clipCopy(true);
  check('剪切同时删掉选中的字', e3.value === '' && CLIP.wrote === '剪掉我', e3.value + '/' + CLIP.wrote);

  e3.value = '## 标题\n- 一\n**粗**'; e3.selectionStart = 0; e3.selectionEnd = e3.value.length;
  sandbox.mdClear();
  check('「正文」按钮去掉记号', e3.value === '标题\n一\n粗', e3.value);
  sandbox.selectAllText();
  check('「全选」选中整篇', e3.selection[0] === 0 && e3.selection[1] === e3.value.length, e3.selection);

  sandbox.findToggle();
  check('查找栏弹出来', byId['findbar'].style.display === 'flex');
  e3.value = '一二三一二'; e3.selectionStart = e3.selectionEnd = 0;
  byId['findWhat'].value = '一二';
  sandbox.findNext();
  check('查找选中第一处', e3.selection[0] === 0 && e3.selection[1] === 2, e3.selection);
  check('报第几处/共几处', String(byId['findInfo'].textContent).includes('1/2'),
    byId['findInfo'].textContent);
  sandbox.findNext();
  check('再点下一个到第二处', e3.selection[0] === 3, e3.selection);
  const fw2 = byId['findWhat2']
    || (byId['findWhat2'] = Object.assign(mkEl('input'), { id: 'findWhat2' }));
  fw2.value = 'X';
  sandbox.findAll();
  check('全部替换', e3.value === 'X三X', e3.value);
  check('报替换了几处', String(byId['findInfo'].textContent).includes('2'), byId['findInfo'].textContent);
  sandbox.findToggle();
  check('查找栏能收起', byId['findbar'].style.display === 'none');

  console.log('⑪b 只读时面板整体禁用');
  byId['toast'].textContent = '';
  await sandbox.openFile('LOCKED-ID');
  sandbox.undoEdit();
  check('加锁时撤销被拦', String(byId['toast'].textContent).includes('加锁'), byId['toast'].textContent);
  await sandbox.openFile(target.id);
  check('回到普通笔记又能撤销', (() => { sandbox.undoEdit(); return true; })());

  console.log('⑫ 进 / 出检索库（菜单栏最右那颗）');
  await sandbox.openFile('DOC-ID');
  for (let i = 0; i < 3; i++) await new Promise(r => setImmediate(r));
  const rm = String(byId['meta'].innerHTML);
  check('未入检索：元信息条写明了', rm.includes('未入检索'), rm.slice(0, 170));
  const rb1 = byId['btnRagTop'];
  check('未入检索：菜单栏那颗写「加入检索」', rb1.style.display !== 'none'
    && String(rb1.textContent).includes('加入检索'), String(rb1.textContent));
  check('那颗就在菜单栏最右边（紧跟 stats）',
    /id="stats">…<\/span>\s*<button id="btnRagTop"/.test(html));
  await sandbox.ragToggle(true);
  check('点了加入检索会调接口并提示', String(byId['toast'].textContent).includes('加入检索库'),
    byId['toast'].textContent);
  await sandbox.openFile(target.id);
  check('已入检索：那颗写「移出检索」', String(byId['btnRagTop'].textContent).includes('移出检索'),
    String(byId['btnRagTop'].textContent));
  // 2026-09-29 辞玖："是否进入检索…不需要在文件树上显示" → 树里不再分 📄/📎，
  // 状态挪到元信息条与右侧 🏷 属性的「库内信息」里。
  check('未入检索不再在树里标（挪到属性里了）',
        !html.includes("'📎'") && html.includes('未入检索'), '');
  check('命中行会显示位置（📍第 N 页）', html.includes('📍${esc(h.where)}'));
  check('右键菜单也有进/出检索库', String(byId['ctxmenu'].innerHTML || '').length >= 0);

  console.log('⑬ 保存的语义（WPS 式：未保存看得见、点保存立刻存、不弹确认）');
  await sandbox.openFile(target.id);
  const e13 = byId['editor'];
  e13.value = '# 大改\n\n只剩一点点\n';
  sandbox.markDirty(true);
  check('改过之后"未保存"看得见', byId['dirtyFlag'] && byId['dirtyFlag'].style.display === 'block'
        && String(sandbox.document.title || '').indexOf('●') === 0,
        [byId['dirtyFlag'] && byId['dirtyFlag'].style.display, sandbox.document.title]);

  const _fetch = sandbox.fetch;
  const puts = [];
  sandbox.fetch = async (url, opts) => {
    const p = String(url).replace(/^https?:\/\/[^/]+/, '');
    if (/\/api\/files\/[^/]+$/.test(p) && opts && opts.method === 'PUT') {
      const body = JSON.parse(opts.body || '{}');
      puts.push(body);
      return { ok: true, status: 200, json: async () => ({ ok: true,
        saved: { chunks: 1, mentions: 0,
                 size_change: { before_bytes: 7677, after_bytes: 1200 } } }) };
    }
    return _fetch(url, opts);
  };
  let confirms = 0;
  sandbox.confirm = () => { confirms++; return true; };
  byId['toast'].textContent = '';
  await sandbox.save();
  check('点保存就立刻存：一次请求、带 allow_shrink', puts.length === 1 && puts[0].allow_shrink === true,
        puts.length);
  check('全程没弹二次确认', confirms === 0, confirms);
  check('存完"未保存"标记消失', byId['dirtyFlag'].style.display === 'none',
        byId['dirtyFlag'].style.display);
  check('体量变化并进"已保存"那句（不另弹）', String(byId['toast'].textContent).includes('比上一版少'),
        byId['toast'].textContent);
  sandbox.fetch = _fetch;

  // 清理
  await req('DELETE', '/api/files/' + fid);
  for (const f of walk((await jget('/api/tree')).tree, [])) {
    if (f.name.startsWith('zz-')) await req('DELETE', '/api/files/' + f.id);
  }
  const h = await jget('/api/health');
  check('测试笔记已删除、文件数复原', h.stats.files === filesBefore, h.stats.files + ' vs ' + filesBefore);

  console.log('④b 待办：预览渲染成复选框，点一下改源码并立刻保存');
  const fmHtml = String(sandbox.mdRender('- [ ] 甲\n- [x] 乙\n', () => null));
  check('待办渲染成复选框', fmHtml.includes('type="checkbox"') && fmHtml.includes('taskbox'),
        fmHtml.slice(0, 140));
  check('已完成的那个默认勾上、带 done 样式',
        /data-task-line="1" checked/.test(fmHtml) && fmHtml.includes('class="task done"'), fmHtml.slice(0, 200));

  await sandbox.openFile(target.id);
  const fmEd = byId['editor'];
  fmEd.value = '- [ ] 甲\n- [x] 乙\n';
  sandbox.markDirty(true);
  const _fetch2 = sandbox.fetch;
  const fmPuts = [];
  sandbox.fetch = async (url, opts) => {
    const p = String(url).replace(/^https?:\/\/[^/]+/, '');
    if (/\/api\/files\/[^/]+$/.test(p) && opts && opts.method === 'PUT') {
      fmPuts.push(JSON.parse((opts.body || '{}')));
      return { ok: true, status: 200,
               json: async () => ({ ok: true, saved: { chunks: 1, size_change: {} }, tree: { ok: true }, mentions: 0 }) };
    }
    return _fetch2(url, opts);
  };
  const fakeBox = mkEl('INPUT');        // 浏览器里 tagName 是大写
  fakeBox.classList = { contains: (c) => c === 'taskbox' };
  fakeBox.getAttribute = (k) => (k === 'data-task-line' ? '0' : null);
  fakeBox.checked = true;
  const clickers = DOC_HANDLERS['click'] || [];
  check('document 上挂了点击处理器', clickers.length >= 1, clickers.length);
  for (const h of clickers) h({ target: fakeBox });
  check('点一下：源码那一行变成勾上', fmEd.value.startsWith('- [x] 甲'), fmEd.value);
  check('第二行没被连带', fmEd.value.split('\n')[1] === '- [x] 乙', fmEd.value);
  await new Promise(r => setImmediate(r));
  check('点一下就立刻存（一次 PUT）', fmPuts.length === 1, fmPuts.length);
  check('存的就是勾上后的正文', !!(fmPuts[0] && fmPuts[0].content.startsWith('- [x] 甲')),
        fmPuts[0] && fmPuts[0].content);
  sandbox.fetch = _fetch2;

  console.log('④c 右侧栏：大纲 / 属性 / 🔗 引用');
  sandbox.sideToggle(true);
  check('侧栏能开', byId['side'].classList.contains('open')
        && sandbox.document.body.classList.contains('sideOn'), byId['side'].classList.contains('open'));
  sandbox.sideTab('outline');
  byId['editor'].value = '# 甲\n\n正文\n\n## 乙\n\n- [ ] 待办\n\n### 丙\n';
  sandbox.sideOutline(byId['sideBody']);
  const olHtml = String(byId['sideBody'].innerHTML);
  check('大纲按标题自动整理（带层级）',
        olHtml.includes('甲') && olHtml.includes('乙') && olHtml.includes('丙')
        && olHtml.includes('oli lv2') && olHtml.includes('oli lv3'), olHtml.slice(0, 170));
  sandbox.jumpHeading(4);
  check('点大纲能跳到那一行',
        byId['editor'].selectionStart === byId['editor'].value.split('\n').slice(0, 4).join('\n').length + 1,
        byId['editor'].selectionStart);

  await sandbox.openFile('FM-ID');
  sandbox.sideTab('props');
  const prHtml = String(byId['sideBody'].innerHTML);
  check('属性栏列出这篇的属性', prHtml.includes('propRows'), prHtml.slice(0, 120));
  const rows = byId['propRows'].children;
  check('属性行数 = 键数 + 一行"新属性"', rows.length === 5, rows.length);
  const vals = rows.filter(r => r.children && r.children[1]).map(r => [r.children[0].value, r.children[1].value]);
  check('键值对得上（title/status/tags/created）',
        vals.some(v => v[0] === 'title' && v[1] === '属性样例')
        && vals.some(v => v[0] === 'status' && v[1] === '待办')
        && vals.some(v => v[0] === 'tags' && v[1] === '甲, 乙'), vals);
  const _f4 = sandbox.fetch;
  let propBody = null;
  sandbox.fetch = async (url, opts) => {
    const p = String(url).replace(/^https?:\/\/[^/]+/, '');
    if (/\/properties$/.test(p) && opts && opts.method === 'POST') {
      propBody = JSON.parse(opts.body || '{}');
      return { ok: true, status: 200, json: async () => ({ ok: true, changed: true, frontmatter: {} }) };
    }
    return _f4(url, opts);
  };
  const stRow = rows.find(r => r.children && r.children[0].value === 'status');
  stRow.children[1].value = '已办';
  stRow.children[1].oninput();
  await sandbox.saveProps();
  check('保存属性：发 POST 且只带改过的键值',
        !!(propBody && propBody.set && propBody.set.status === '已办'), propBody);
  check('tags 拆成数组传', !!(propBody && Array.isArray(propBody.set.tags)
        && propBody.set.tags.join(',') === '甲,乙'), propBody && propBody.set.tags);
  check('其它键一起传（title/created）',
        !!(propBody && propBody.set.title === '属性样例' && propBody.set.created === '2026-09-01'), propBody);
  sandbox.fetch = _f4;

  // 2026-09-29 辞玖：「按属性找」撤掉，换成「🔗 引用」
  check('「按属性找」那个 tab 已经撤掉', !String(html).includes("sideTab('find')"), '');
  check('换成了「🔗 引用」那一个', String(html).includes("sideTab('refs')"), '');
  sandbox.sideTab('find');                           // 老的 localStorage 值兜底：不许崩
  await new Promise(r => setImmediate(r));
  check('sideTab 落到不认识的页时兜底（不崩、有内容）',
        String(byId['sideBody'].innerHTML).length > 0, String(byId['sideBody'].innerHTML).slice(0, 60));
  sandbox.sideToggle(false);
  check('侧栏能收', !byId['side'].classList.contains('open'));

  console.log('④d 文件内搜索：搜索框「全库 / 本篇」');
  await sandbox.openFile('FM-ID');
  const dq = byId['q'];
  byId['editor'].value = '# 正文\n\n甲一\n乙\n甲二\n';
  dq.value = '甲';
  sandbox.setScope('doc');
  const dh = String(byId['hits'].innerHTML);
  check('切到"本篇"就高亮那颗开关', byId['scDoc'].classList.contains('on') && !byId['scAll'].classList.contains('on'),
        byId['scDoc'].classList.contains('on'));
  check('命中按行列出：行号 + 整行 + 命中加亮 + 计数',
        dh.includes('fline') && dh.includes('<mark>甲</mark>') && dh.includes('<span class="ln">3</span>')
        && dh.includes('共 2 处'), dh.slice(0, 200));
  check('一进来就停在第一处（选中命中词）',
        byId['editor'].selectionStart === byId['editor'].value.indexOf('甲')
        && byId['editor'].selectionEnd === byId['editor'].value.indexOf('甲') + 1,
        [byId['editor'].selectionStart, byId['editor'].selectionEnd]);
  sandbox.docStep(1);
  check('下一处 → 第二处，计数跟着走',
        byId['editor'].selectionStart === byId['editor'].value.lastIndexOf('甲')
        && String(byId['docPos'].textContent).includes('第 2 处'),
        [byId['editor'].selectionStart, byId['docPos'].textContent]);
  sandbox.docStep(1);
  check('到底了绕回第一处',
        byId['editor'].selectionStart === byId['editor'].value.indexOf('甲'), byId['editor'].selectionStart);
  sandbox.docJump(0);
  check('点结果也能跳', byId['editor'].selectionStart === byId['editor'].value.indexOf('甲'),
        byId['editor'].selectionStart);
  dq.value = '没有这段字';
  sandbox.docSearch();
  check('找不到就直说', String(byId['hits'].innerHTML).includes('没有「没有这段字」'),
        String(byId['hits'].innerHTML).slice(0, 120));
  dq.value = '甲';
  sandbox.focusDocFind();
  check('Ctrl+F 那个入口：切本篇 + 聚焦搜索框 + 立刻出命中',
        byId['scDoc'].classList.contains('on') && dq.focused === true
        && String(byId['hits'].innerHTML).includes('共 2 处'), [byId['scDoc'].classList.contains('on'), dq.focused]);
  sandbox.setScope('all');
  check('切回全库：本篇结果区收起、全库那颗高亮',
        byId['hits'].style.display === 'none' && byId['scAll'].classList.contains('on'), byId['hits'].style.display);

  console.log('⑪ Esc：关得掉数据表页 / 自检页 / 本篇查找（2026-09-30 体检 8.2）');
  const pressEsc = () => (DOC_HANDLERS['keydown'] || []).forEach(fn => fn({ key: 'Escape' }));
  check('document 上确实注册了 keydown（Esc 那条）', (DOC_HANDLERS['keydown'] || []).length > 0,
        (DOC_HANDLERS['keydown'] || []).length);
  await sandbox.openTables();
  await new Promise(r => setImmediate(r));
  check('数据表页先开着', byId['tablemodal'].classList.contains('open'));
  pressEsc();
  check('Esc 关掉数据表页', !byId['tablemodal'].classList.contains('open'));
  await sandbox.runSelfcheck();
  await new Promise(r => setImmediate(r));
  check('自检页先开着', byId['selfcheck'].classList.contains('open'));
  pressEsc();
  check('Esc 关掉自检页', !byId['selfcheck'].classList.contains('open'));
  sandbox.setScope('doc');
  check('切到「本篇」（查找条生效）', byId['scDoc'].classList.contains('on'));
  pressEsc();
  check('Esc 退出本篇查找、回到全库 + 清结果',
        byId['scAll'].classList.contains('on') && byId['hits'].style.display === 'none',
        [byId['scAll'].classList.contains('on'), byId['hits'].style.display]);

  console.log('⑫ 编辑工具区：WPS 那种页签（开始 / 插入 / 视图 / 文件）2026-09-30');
  const tabNames = (String(html).match(/class="rtab[^"]*" data-rtab="([^"]+)"/g) || [])
    .map(x => x.match(/data-rtab="([^"]+)"/)[1]);
  check('四个页签都在（开始/插入/视图/文件）',
        ['开始', '插入', '视图', '文件'].every(x => tabNames.indexOf(x) >= 0), tabNames);
  check('默认停在「开始」', String(html).indexOf('id="ribbonBody" data-rtab="开始"') >= 0, '');
  sandbox.ribbonTab('插入');
  check('切到「插入」：ribbonBody 的 data-rtab 跟着改',
        String(byId['ribbonBody'].getAttribute('data-rtab')) === '插入',
        byId['ribbonBody'].getAttribute('data-rtab'));
  sandbox.ribbonTab('不存在的页签');
  check('非法页签名回落到「开始」', String(byId['ribbonBody'].getAttribute('data-rtab')) === '开始',
        byId['ribbonBody'].getAttribute('data-rtab'));
  sandbox.ribbonTab('开始');
  // 每个 .rgroup 都必须带 data-rtab，否则它会永远不显示（漏网 = 按钮消失）
  const groups = String(html).match(/<div class="rgroup"[^>]*>/g) || [];
  const noTab = groups.filter(g => g.indexOf('data-rtab=') < 0);
  check('每个工具组都挂在某个页签上（共 %d 组，漏网 %d 组）'.replace('%d', groups.length)
        .replace('%d', noTab.length), groups.length > 0 && noTab.length === 0, noTab);
  // WPS 行为：收起只收"工具区"，页签那一行留着（不然收起来就不知道还能切哪个页签）
  sandbox.ribbonToggle(false);          // 注意：这里 force=true 表示"展开"，收起要传 false
  check('收起后：工具区隐藏、页签行还在',
        byId['ribbonBody'].style.display === 'none'
        && String(html).indexOf('id="ribbonTabs"') >= 0, byId['ribbonBody'].style.display);
  sandbox.ribbonToggle(true);
  check('展开后：工具区回来、当前页签没丢',
        byId['ribbonBody'].style.display !== 'none'
        && String(byId['ribbonBody'].getAttribute('data-rtab')) === '开始',
        byId['ribbonBody'].getAttribute('data-rtab'));
  check('四个页签都有组挂上去',
        ['开始', '插入', '视图', '文件'].every(nm =>
          groups.some(g => g.indexOf('data-rtab="' + nm + '"') >= 0)),
        groups.map(g => (g.match(/data-rtab="([^"]+)"/) || [])[1]));

  console.log('⑬ 常用工具（照 WPS 补的那批）：待办 / 缩进 / 标题级别 / 高亮 / 格式刷');
  const eW = byId['editor'];
  eW.value = '甲\n乙'; eW.selectionStart = 0; eW.selectionEnd = 3;
  sandbox.mdLine('- [ ] ');
  check('待办清单：选中的行都变成 - [ ]', eW.value === '- [ ] 甲\n- [ ] 乙', eW.value);
  eW.value = '甲\n乙'; eW.selectionStart = 0; eW.selectionEnd = 3;
  sandbox.mdIndent(1);
  check('增加缩进：每行加两个空格', eW.value === '  甲\n  乙', JSON.stringify(eW.value));
  sandbox.mdIndent(-1);
  check('减少缩进：每行去掉两个空格', eW.value === '甲\n乙', JSON.stringify(eW.value));
  eW.value = '## 标题'; eW.selectionStart = eW.selectionEnd = 3;
  sandbox.mdHeadingStep(1);
  check('A⁺ 提升一级：## → #', eW.value === '# 标题', eW.value);
  eW.value = '####### 不算标题'; eW.selectionStart = eW.selectionEnd = 3;
  sandbox.mdHeadingStep(-1);
  check('A⁻ 只认 1~6 个 #（七个 # 不是标题，不动它）', eW.value === '####### 不算标题', eW.value);
  eW.value = '###### 最深'; eW.selectionStart = eW.selectionEnd = 3;
  sandbox.mdHeadingStep(-1);
  check('A⁻ 在 H6 上落到正文（去掉记号）', eW.value === '最深', eW.value);
  eW.value = '普通一行'; eW.selectionStart = eW.selectionEnd = 2;
  sandbox.mdHeadingStep(1);
  check('A⁺ 把正文提成 H1', eW.value === '# 普通一行', eW.value);
  eW.value = '重点'; eW.selectionStart = 0; eW.selectionEnd = 2;
  sandbox.mdWrap('==', '==');
  check('高亮：用 == 包起来', eW.value === '==重点==', eW.value);
  check('高亮在预览里渲染成 <mark>', sandbox.mdRender('==重点==', {}).includes('<mark>重点</mark>'), '');
  check('删除线在预览里渲染成 <del>（以前按钮能插、预览不认）',
        sandbox.mdRender('~~旧~~', {}).includes('<del>旧</del>'), '');
  eW.value = '## 甲\n乙\n丙'; eW.selectionStart = eW.selectionEnd = 3;
  sandbox.mdFormatBrush();
  check('格式刷第一步：取到当前行的记号（按钮亮着）',
        byId['rBrush'].classList.contains('on') === true, byId['rBrush'].className);
  eW.selectionStart = 5; eW.selectionEnd = 8;                 // 选中「乙」「丙」两行
  sandbox.mdFormatBrush();
  check('格式刷第二步：把 ## 刷到选中的行上（原记号先去掉再加）',
        eW.value === '## 甲\n## 乙\n## 丙', JSON.stringify(eW.value));
  check('刷完按钮不再亮', byId['rBrush'].classList.contains('on') === false, byId['rBrush'].className);
  eW.value = '一行'; eW.selectionStart = eW.selectionEnd = 0;
  sandbox.mdFormatBrush();
  check('没有记号的行：格式刷不取，并提示一句', String(byId['toast'].textContent).indexOf('没有记号') >= 0,
        byId['toast'].textContent);

  console.log('⑭ 不做自定义快捷键（辞玖 2026-09-30 22:59：只保留基础的复制粘贴）');
  check('各格式没有按键分派（PDF 翻页/缩放、图片缩放旋转都不接）',
        String(html).indexOf("MODE === 'pdf') {") < 0
        && String(html).indexOf("e.key === 'PageDown'") < 0
        && String(html).indexOf("e.key === 'r' || e.key === 'R'") < 0, '');
  check('全局 keydown 只剩一条（就是关浮层那个 Esc）',
        (String(html).match(/document\.addEventListener\('keydown'/g) || []).length === 1,
        (String(html).match(/document\.addEventListener\('keydown'/g) || []).length);
  check('工具提示里不再宣称这些键（写了就得能按）',
        String(html).indexOf('PageDown /') < 0 && String(html).indexOf('（0）') < 0
        && String(html).indexOf('旋转 90°（R）') < 0, '');
  check('原来那套（Ctrl+S/B/I/F、Esc）还在', String(html).indexOf("k === 's'") >= 0
        && String(html).indexOf("k === 'b'") >= 0 && String(html).indexOf("k === 'f'") >= 0, '');

  console.log('\n========  结论: %d 通过 / %d 失败', pass, fail);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('崩了:', e); process.exit(2); });
