// 命名包控制台面板冒烟测试（Node + vm，DOM 打桩）：只验"渲染与调用链不炸、内容对"
const fs = require('fs');
const vm = require('vm');

const code = fs.readFileSync(require('path').join(__dirname, 'app.js'), 'utf8');
const fails = [];
function check(name, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '  ← ' + extra));
  if (!cond) fails.push(name);
}

function el(id) {
  const attrs = {};
  return {
    id, innerHTML: '', textContent: '', value: '', style: {}, dataset: {}, tagName: 'DIV',
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    querySelectorAll: () => [], appendChild() {}, addEventListener() {}, focus() {},
    setAttribute(k, v) { attrs[k] = v; }, getAttribute(k) { return attrs[k] === undefined ? null : attrs[k]; },
  };
}
const els = {};
const document = {
  getElementById: (id) => (els[id] = els[id] || el(id)),
  querySelectorAll: () => [], addEventListener: () => {}, createElement: (t) => el(t), body: el('body'),
};

// 库的树形状（/notes/api/tree）：{name, children:[...], files:[...]}
const LIB_TREE = { name: '', files: [], children: [
  { name: 'vault', files: [], children: [
    { name: '项目', files: [{ id: 'f1', name: '待办清单.md', size: 2048, kind: 'markdown' }], children: [] },
    { name: '记忆', files: [{ id: 'f2', name: '原则区.md', size: 4096, kind: 'markdown' }], children: [] },
  ] },
] };

const VAULT_TREE = { name: '辞玖', path: '', type: 'dir', children: [
  { name: '项目', path: '项目/', type: 'dir', children: [
    { name: '待办清单.md', path: '项目/待办清单.md', type: 'file', size: 2048 },
  ] },
  { name: '记忆', path: '记忆/', type: 'dir', children: [
    { name: '原则区.md', path: '记忆/原则区.md', type: 'file', size: 4096 },
  ] },
] };

const PACK_PATH = 'vault/项目/待办清单.md';
const seen = [];
function fetchStub(url, opts) {
  seen.push(url);
  let body = {};
  if (url.endsWith('/api/bridge/plugins/call')) {
    const req = JSON.parse(opts.body);
    if (req.tool === 'pack.list') {
      body = { ok: true, data: {
        packs: [{ id: 'pack-1', name: '测试包', owner: 'agent.dsh', enabled: true,
                  files: [{ path: 'vault/项目/待办清单.md', enabled: true }], files_missing: [] }],
        agents: [{ agent: 'agent.dsh', enabled: true }],
        path: 'data/context/packs.json', file_chars: 6000 } };
    } else if (req.tool === 'pack.preview') {
      body = { ok: true, data: { files: [{ path: '项目/待办清单.md', ok: true, chars: 12, head: 'hello-vault' }] } };
    } else if (req.tool === 'pack.create') {
      body = { ok: true, data: { pack: { id: 'pack-2' } } };
    } else if (req.tool === 'pack.delete') {
      body = { ok: true, data: { deleted: 'pack-1' } };
    } else { body = { ok: false, error: 'unknown tool' }; }
  } else if (url.endsWith('/api/context/routes')) {
    body = [{ agent: 'agent.dsh', enabled: true, docs: ['profile'], packBytes: 1024, packUpdated: '2026-09-13T00:00:00+08:00' }];
  } else if (url.endsWith('/notes/api/tree')) {
    body = { ok: true, tree: LIB_TREE };          // 选择器优先问库
  } else if (url.endsWith('/api/vault/list')) {
    body = VAULT_TREE;                            // 库不可用时的兜底
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
}

const ctx = {
  document, window: {}, location: { hash: '' }, fetch: fetchStub, console,
  setTimeout, setInterval: () => 0, clearInterval: () => {}, clearTimeout,
  alert: () => {}, confirm: () => true,
  localStorage: { getItem: () => null, setItem: () => {} },
  EventSource: function () { this.close = () => {}; this.addEventListener = () => {}; },
};
vm.createContext(ctx);

(async () => {
  try {
    vm.runInContext(code, ctx);
  } catch (e) {
    check('app.js 整体求值不抛错', false, e.message);
    console.log('\n结果：加载即失败'); process.exit(1);
  }
  check('app.js 整体求值不抛错', true);

  const html = ctx.renderContextRoutesPanel();
  check('注入包面板含包编辑区与看板容器', html.includes('packFormBox') && html.includes('ctxBoardBox'));
  check('面板文案带「从库中选择文件」', html.includes('从库中选择文件'));
  check('面板模板闭合（以反引号结尾的 return 正常）', html.trim().endsWith('</div>'));

  await ctx.loadCtxPanel();
  check('调的是插件工具通道', seen.some(u => u.endsWith('/api/bridge/plugins/call')));
  ctx.window.__ctxExpanded = ctx.window.__ctxExpanded || {};
  ctx.window.__ctxExpanded['folder:agent.dsh'] = true;  // 展开 agent 文件夹
  ctx.window.__ctxExpanded['pack:pack-1'] = true;       // 展开命名包才渲染文件行
  ctx.renderCtxBoard();
  const board = els.ctxBoardBox.innerHTML;
  if (process.env.DBG) console.log('--- 看板 HTML ---\n' + board.slice(0, 1200));
  check('看板渲染出包名', board.includes('测试包'), board.slice(0, 200));
  check('看板显示文件名', board.includes('项目/待办清单.md'), board.slice(0, 300));
  check('看板显示目标 agent', board.includes('agent.dsh'));
  check('卡片上有「从库中选择文件」入口', board.includes('从库中选择文件'));

  await ctx.openPackForm('');
  check('新建表单出现包名输入框', els.packFormBox.innerHTML.includes('id="packName"'));
  check('新建表单有启用开关', els.packFormBox.innerHTML.includes('packEnabled'));
  check('新建表单说明改动在卡片上做', els.packFormBox.innerHTML.includes('卡片上即时增删文件'));
  check('默认勾上全部 agent（新包=全部）', els.packFormBox.innerHTML.includes('checked'));
  check('表单默认无 id（走 create）', ctx.window.__packEdit && ctx.window.__packEdit.id === '');

  await ctx.openPackForm('pack-1');
  check('编辑态带出包名', els.packFormBox.innerHTML.includes('测试包'));
  check('编辑态带出文件名', els.packFiles.innerHTML.includes('项目/待办清单.md'), els.packFiles.innerHTML);
  check('编辑态记住 id', ctx.window.__packEdit.id === 'pack-1');

  await ctx.previewPackFiles();
  check('预览渲染出文件内容', els.packPreview.innerHTML.includes('hello-vault'), els.packPreview.innerHTML.slice(0, 200));
  check('预览显示字符数', els.packPreview.innerHTML.includes('12 字符'));

  await ctx.openVaultPicker();
  const pick1 = els.vaultPickBox.innerHTML;
  check('选文件弹窗渲染出库的树（顶层文件夹在）', pick1.includes('vault'), pick1.slice(0, 160));
  check('弹窗标了内容来源是库', pick1.includes('来源：库'), pick1.slice(0, 120));
  check('选文件弹窗只列文件不列目录行', !pick1.includes('>项目/<'));

  // ── 文件树默认收起（辞玖 2026-10-01：「注入包模块添加文件时文件树应默认收起」）──
  check('默认收起：文件一个都不渲染', !pick1.includes('待办清单.md') && !pick1.includes('原则区.md'),
    pick1.slice(0, 240));
  check('默认收起：顶层文件夹是收起图标 📁', pick1.includes('📁 vault') && !pick1.includes('📂 vault'),
    pick1.slice(0, 160));
  check('默认收起：下一层目录也不渲染', !pick1.includes('项目') && !pick1.includes('记忆'),
    pick1.slice(0, 200));

  ctx.toggleVaultDir(encodeURIComponent('vault'));       // 手动点开一层
  const pick2 = els.vaultPickBox.innerHTML;
  check('点开顶层文件夹才出现下一层目录', pick2.includes('记忆') && pick2.includes('📁 记忆'),
    pick2.slice(0, 220));
  check('下一层里的文件仍然收起（要再点一次）', !pick2.includes('原则区.md'), pick2.slice(0, 220));
  ctx.toggleVaultDir(encodeURIComponent('vault'));       // 收回去

  els.vaultPickFilter.value = '原则区';                   // 筛选：命中路径要自动展开
  ctx.renderVaultPick();
  const pick3 = els.vaultPickBox.innerHTML;
  check('筛选时自动展开命中路径（能看见文件）', pick3.includes('原则区.md'), pick3.slice(0, 240));
  check('筛选没把无关那一支也展开', !pick3.includes('待办清单.md'), pick3.slice(0, 240));
  els.vaultPickFilter.value = '';
  ctx.renderVaultPick();
  check('打开时清单里就是 1 个文件', ctx.window.__packEdit.files.length === 1,
    JSON.stringify(ctx.window.__packEdit.files));
  check('弹窗给选中文件打了勾', els.vaultPickBox.innerHTML.includes('checked'));
  check('筛选输入框未被重建（在外面）', !!els.vaultPickFilter);

  ctx.pickToggle('vault/记忆/原则区.md', true);
  check('勾选后文件进清单', ctx.window.__packEdit.files.length === 2, JSON.stringify(ctx.window.__packEdit.files));
  check('勾选后表单同步显示', els.packFiles.innerHTML.includes('记忆/原则区.md'));
  ctx.pickToggle('vault/记忆/原则区.md', false);
  check('取消勾选后移除', ctx.window.__packEdit.files.length === 1);

  ctx.closeVaultPicker(false);
  const pathsNow = ctx.fileEntries(ctx.window.__packEdit.files).map(e => e.path);
  check('取消选择回滚到打开前的清单', JSON.stringify(pathsNow) === JSON.stringify([PACK_PATH]),
    JSON.stringify(pathsNow));
  check('弹窗关闭（移除 show）', true);

  ctx.window.__packEdit = { id: '', files: ['项目/待办清单.md'] };
  els.packName = el('packName'); els.packName.value = '新包';
  const realQ = document.querySelectorAll;
  document.querySelectorAll = () => [];   // 无 agent 勾选 → agents 空数组
  await ctx.savePack();
  check('新建包走 pack.create 并在成功后收起表单', els.packFormBox.innerHTML === '', els.packFormBox.innerHTML.slice(0, 120));
  document.querySelectorAll = realQ;

  // ── 文件树：首屏所有文件夹默认收起（2026-09-13 辞玖指示）──────────
  await ctx.loadTree();
  const tree1 = els.fileTree.innerHTML;
  check('树渲染出顶层文件夹行', tree1.includes('项目') && tree1.includes('记忆'), tree1.slice(0, 160));
  check('顶层文件夹是收起箭头 ▸', tree1.includes('▸'), tree1.slice(0, 160));
  check('没有展开箭头 ▾', !tree1.includes('▾'), tree1.slice(0, 160));
  check('收起时子文件不渲染（省 DOM）', !tree1.includes('待办清单.md'), tree1.slice(0, 200));
  check('根的直接子级在最外层（深度 0 容器不收起）', /class="tree-children"[^>]*>\s*<div class="tree-row dir"/.test(tree1),
    tree1.slice(0, 120));

  ctx.toggleDir('项目/', null);         // 展开一个（内部 loadTree 是异步重渲染，要等一拍）
  await new Promise(r => setTimeout(r, 30));
  const tree2 = els.fileTree.innerHTML;
  check('展开后能看到该文件夹里的文件', tree2.includes('待办清单.md'), tree2.slice(0, 200));
  check('展开的那个换成 ▾', tree2.includes('▾'));
  check('其它文件夹仍保持收起', !tree2.includes('原则区.md'), tree2.slice(0, 200));

  await ctx.loadTree();                 // 再整体重渲染一次：用户展开状态必须被记住
  const tree3 = els.fileTree.innerHTML;
  check('重渲染后仍记得展开状态（不会又全收起）', tree3.includes('待办清单.md') && tree3.includes('▾'),
    tree3.slice(0, 200));

  console.log('\n结果：' + (fails.length ? fails.length + ' 项失败 → ' + fails.join(' / ') : '全部通过'));
  process.exit(fails.length ? 1 : 0);
})();




