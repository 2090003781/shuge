/**
 * 枢阁工具集 / 权限开关的自测（不需要 DSH，用假 ctx 跑）。
 *   node test/library-tools.test.mjs
 * 会在临时配置上跑权限改动，不动真 config.json。
 */
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const dir = mkdtempSync(join(tmpdir(), 'brain-test-'));
const cfgPath = join(dir, 'config.json');
writeFileSync(cfgPath, JSON.stringify({ libraryUrl: 'http://127.0.0.1:8848', tools: { permissions: {} } }), 'utf8');
process.env.BRAIN_CONFIG_PATH = cfgPath;

const M = await import('../src/library-tools.js');

let pass = 0; let fail = 0;
const check = (name, cond, extra = '') => {
  if (cond) { pass += 1; console.log(`  ✅ ${name}${extra ? ` | ${extra}` : ''}`); }
  else { fail += 1; console.log(`  ❌ ${name}${extra ? ` | ${extra}` : ''}`); }
};

function makeCtx() {
  const tools = new Map();
  const cmds = [];
  return {
    tools: { register: (def) => { tools.set(def.name, def); return () => tools.delete(def.name); } },
    commands: { register: (c) => { cmds.push(c); return () => {}; } },
    inject(_deps, fn) { fn(this); return () => {}; },
    _tools: tools, _cmds: cmds,
  };
}

const cfg = { libraryUrl: 'http://127.0.0.1:8848', tools: { permissions: {} } };

console.log('① 按危险程度分档');
const levels = new Set(M.LEVELS.map((l) => l.id));
check('每个工具都归了档', M.TOOLS.every((t) => levels.has(t.level)));
for (const lv of M.LEVELS) {
  check(`档 ${lv.id} 有工具`, M.TOOLS.some((t) => t.level === lv.id),
    M.TOOLS.filter((t) => t.level === lv.id).map((t) => t.name).join(','));
}
check('每个工具都有入参 schema', M.TOOLS.every((t) => t.parameters && t.parameters.type === 'object'));
check('就三档：只读 / 可写 / 相信', JSON.stringify(M.LEVEL_IDS) === '["read","write","trust"]',
  M.LEVEL_IDS.join(','));

check('每个入口都标了它对应的软件操作（op）', M.TOOLS.every((t) => /^(GET|POST|PUT|DELETE) \/api\//.test(t.op || '')),
  M.TOOLS.filter((t) => !t.op).map((t) => t.name).join(','));
{
  // 跨仓对账：入口指向的接口必须真在库的接口清单里（docs/openapi.json）
  const specPath = process.env.LIB_OPENAPI
    || 'D:/core-notes/docs/openapi.json';
  try {
    const spec = JSON.parse(readFileSync(specPath, 'utf8'));
    const paths = spec.paths || {};
    const bad = M.TOOLS.filter((t) => {
      const [method, rawPath] = t.op.split(' ');
      const p = rawPath.replace(/\{id\}/g, '{id}');
      const item = paths[p];
      return !item || !item[method.toLowerCase()];
    }).map((t) => `${t.name}→${t.op}`);
    check('入口指向的接口都在库的接口清单里', bad.length === 0, bad.slice(0, 4).join(' '));
  } catch (e) {
    check('读到库的接口清单', false, String(e?.message ?? e).slice(0, 80));
  }
}

console.log('② 按权限释放（默认：read/write 开，risky/danger 关）');
const ctx = makeCtx();
const reg = M.registerLibraryTools(ctx, cfg);
check('只注册放开的那两档', ctx._tools.size === M.released(cfg).length && ctx._tools.size > 0,
  `${ctx._tools.size} 个`);
check('只读工具在', ctx._tools.has('brain_search') && ctx._tools.has('brain_health'));
check('写入工具在', ctx._tools.has('brain_create') && ctx._tools.has('brain_update'));
check('risky 工具默认收起', !ctx._tools.has('brain_rag') && !ctx._tools.has('brain_reindex'));
check('危险工具默认收起', !ctx._tools.has('brain_delete') && !ctx._tools.has('brain_lock'));
check('权限表能打印', M.permissionTable(cfg).includes('brain_delete'));

console.log('③ 三档累积：放到 trust → 全给；收到 read → 只剩只读');
cfg.tools.permissions = { level: 'trust', deny: [], allow: [] };
reg.sync();
check('放到相信档：危险入口也给了',
  ctx._tools.has('brain_delete') && ctx._tools.has('brain_visibility') && ctx._tools.has('brain_create'),
  ctx._tools.size);
cfg.tools.permissions = { level: 'read', deny: [], allow: [] };
reg.sync();
check('收到只读档：写入/危险都收回',
  ctx._tools.has('brain_search') && !ctx._tools.has('brain_create') && !ctx._tools.has('brain_delete'),
  ctx._tools.size);
cfg.tools.permissions = { level: 'write', deny: [], allow: [] };
reg.sync();
check('放回可写档', ctx._tools.has('brain_create') && !ctx._tools.has('brain_delete'));

console.log('④ 单个工具拉黑 / 放行');
cfg.tools.permissions = { level: 'write', deny: ['brain_search'], allow: [] };
reg.sync();
check('拉黑生效', !ctx._tools.has('brain_search'));
cfg.tools.permissions = { level: 'read', deny: [], allow: ['brain_delete'] };
reg.sync();
check('单独放行也能生效（低档也能单放一个危险入口）',
  ctx._tools.has('brain_delete') && !ctx._tools.has('brain_create'));

console.log('⑤ 真调一次（连本机库）');
try {
  const out = await ctx._tools.get('brain_health').execute({});
  const text = out.content?.[0]?.text ?? '';
  check('brain_health 拿回统计', text.includes('"files"'), text.slice(0, 80));
} catch (e) {
  check('brain_health 拿回统计', false, String(e?.message ?? e));
}
try {
  await ctx._tools.get('brain_read').execute({ path: '不存在的路径zzz' });
  check('路径不存在时报错', false, '居然没报错');
} catch (e) {
  check('路径不存在时报错（错误不被吞）', /not in library|no such file|404/.test(String(e?.message ?? e)),
    String(e?.message ?? e).slice(0, 80));
}

console.log('⑥ /brain 命令（控制台里看/改权限）');
const ctx2 = makeCtx();
M.registerBrainCommand(ctx2, cfg, { onPermChange: () => reg.sync() });
check('命令名是 brain', ctx2._cmds[0]?.name === 'brain');
const t1 = await ctx2._cmds[0].handler({ input: { text: '' } });
check('不带参数 → 权限表',
  t1.kind === 'text' && t1.text.includes('现在放到：') && t1.text.includes('brain_delete'), t1.text.split('\n')[0]);
const t2 = await ctx2._cmds[0].handler({ input: { text: 'perm read' } });
check('能切到只读档', t2.kind === 'text'
  && JSON.parse(readFileSync(cfgPath, 'utf8')).tools.permissions.level === 'read');
const t3 = await ctx2._cmds[0].handler({ input: { text: 'perm deny brain_health' } });
check('能单独拉黑并写回配置',
  t3.kind === 'text' && JSON.parse(readFileSync(cfgPath, 'utf8')).tools.permissions.deny.includes('brain_health'));
const t4 = await ctx2._cmds[0].handler({ input: { text: 'perm trust' } });
check('能切到相信档', t4.kind === 'text'
  && JSON.parse(readFileSync(cfgPath, 'utf8')).tools.permissions.level === 'trust');
const t5 = await ctx2._cmds[0].handler({ input: { text: 'tools' } });
check('tools 子命令列工具', t5.kind === 'text' && t5.text.includes('brain_'));
const t6 = await ctx2._cmds[0].handler({ input: { text: '胡说' } });
check('不认识的子命令给用法', t6.kind === 'error');
const t7 = await ctx2._cmds[0].handler({ input: { text: 'status' } });
check('status 子命令连库', t7.kind === 'text' && t7.text.includes('库：'), t7.text.split('\n')[0]);

console.log('⑦ 权限挂到 DSH 设置服务（控制台卡片的数据面）');
const scopeCalls = [];
const fakeScope = {
  _v: { level: 'write', deny: [], allow: [] },
  _watch: null,
  get() { return { ...this._v }; },
  watch(cb) { this._watch = cb; },
  _base: { level: 'write', deny: [], allow: [] },
  update(patch) { this._v = { ...this._v, ...patch }; scopeCalls.push(patch); this._watch?.({ ...this._v }); },
  // 真语义：replace({}) 是"清空用户层"→ 回落到 base/schema 默认
  replace(p) { this._v = { ...this._base, ...p }; this._watch?.({ ...this._v }); },
};
const cfg2 = { libraryUrl: 'http://127.0.0.1:8848', tools: { permissions: { level: 'write', deny: [], allow: [] } } };
const ctx3 = makeCtx();
let synced = 0;
let reg3 = null;                       // 先声明：变更回调要能调到它
const att = M.attachSettings({ settings: { register: (ns, schema, opts) => {
  check('注册的命名空间是 second-brain-permissions', ns === 'second-brain-permissions', ns);
  check('base 用的是部署配置里的权限（三档）', opts?.base?.level === 'write');
  return fakeScope;
} } }, cfg2, () => { synced += 1; reg3?.sync(); });
check('挂载成功', att.ok === true, att.reason ?? '');
reg3 = M.registerLibraryTools(ctx3, cfg2);
check('按设置服务里的值释放（危险档没开）', !ctx3._tools.has('brain_delete') && ctx3._tools.has('brain_search'));
fakeScope.update({ level: 'trust' });
check('设置文档一改，工具立刻同步', ctx3._tools.has('brain_delete') && synced > 0, `synced=${synced}`);
const cmdRes = await M.changePerm(cfg2, ['trust']);
check('权限命令写的是设置文档（不是 config.json）',
  cmdRes.changed && scopeCalls.some((p) => p.level === 'trust'), JSON.stringify(scopeCalls.slice(-1)));
M.resetPerms();
check('重置清空用户层（回落到 base = write）', fakeScope._v.level === 'write', JSON.stringify(fakeScope._v));

console.log('⑧ 浏览器半侧：卡片会注册到 settings.plugin.item');
const clientPath = new URL('../src/client.js', import.meta.url).pathname.replace(/^\//, '');
const clientSrc = readFileSync(decodeURIComponent(clientPath), 'utf8');
const registered = [];
globalThis.window = {
  __ModuleLoader__: { load: ({ id, factory }) => { registered.push({ id, factory }); } },
};
const fakeReact = {
  createElement: (type, props, ...kids) => ({ type, props: props ?? {}, kids: kids.flat() }),
  useState: (v) => [v, () => {}],
  useEffect: () => {},
};
const fakeRequire = (name) => {
  if (name === 'react') return fakeReact;
  throw new Error(`不该 require：${name}`);
};
await import(new URL('../src/client.js', import.meta.url).href);
check('客户端 bundle 用 __ModuleLoader__ 注册了自己', registered.length === 1 && registered[0].id === 'dsh-second-brain');
const mod = registered[0].factory(fakeRequire);
check('导出 apply 与 inject', typeof mod.apply === 'function' && Array.isArray(mod.inject));
const slotCalls = [];
const ctx4 = {
  settingsScope: { bind: ({ namespace }) => { slotCalls.push(`bind:${namespace}`); return fakeScope; } },
  slots: {
    inject: (key, fn) => { slotCalls.push(`inject:${key}`); return fn(); },
    register: (options, component) => { slotCalls.push(`register:${options.key}`); slotCalls.push(component); },
  },
};
mod.apply(ctx4);
check('绑定的命名空间一致', slotCalls.includes('bind:second-brain-permissions'), slotCalls[0]);
check('注册进 settings.plugin.item 槽', slotCalls.includes('inject:settings.plugin.item'));
check('卡片的 key = 命名空间（宿主才会派发）', slotCalls.includes('register:second-brain-permissions'));
const card = slotCalls.find((x) => typeof x === 'function');
const tree = card({ value: { level: 'write' }, actions: { set() {}, reset() {} } });
const flat = JSON.stringify(tree);
check('卡片渲染出 3 个档位单选框', (flat.match(/radio/g) || []).length === 3, (flat.match(/radio/g) || []).length);
check('三档名字是 只读 / 可写 / 相信', flat.includes('只读') && flat.includes('可写') && flat.includes('相信'));
check('卡片有保存与重置', flat.includes('保存') && flat.includes('重置'));
check('卡片写明库页面按钮不受影响', flat.includes('按钮不受影响'));

console.log(`\n========  结论：${fail ? '有失败项' : '全部通过'}（${pass} 通过 / ${fail} 失败）`);
process.exitCode = fail ? 1 : 0;   // 不用 process.exit：Windows 上会把未收尾的句柄打成断言噪音
