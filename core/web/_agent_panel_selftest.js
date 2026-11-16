// Agent 接入面板自测（Node + vm + DOM 打桩）：验"渲染与调用链不炸、内容对"。
//   跑：node web/_agent_panel_selftest.js
// 为什么不进浏览器：控制台其余面板也是这么验的（见 web/_panel_selftest.js）。
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const code = fs.readFileSync(path.join(__dirname, 'agents.js'), 'utf8');
const fails = [];
function check(name, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '  ← ' + extra));
  if (!cond) fails.push(name);
}

// ---- DOM 打桩 ----
function mkEl(id) {
  const el = {
    id, value: '', style: {}, disabled: false, checked: false, indeterminate: false,
    className: '', children: [], dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute() {}, getAttribute: () => null, addEventListener() {}, appendChild() {},
    focus() {}, select() {}, querySelectorAll: () => [],
  };
  let _text = '', _html = '';
  Object.defineProperty(el, 'textContent', {
    get: () => _text,
    set: (v) => { _text = String(v == null ? '' : v); _html = _text.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])); },
  });
  Object.defineProperty(el, 'innerHTML', {
    get: () => _html,
    set: (v) => { _html = String(v == null ? '' : v); _text = _html.replace(/<[^>]*>/g, ''); },
  });
  return el;
}
const els = {};
function getEl(id) { if (!els[id]) els[id] = mkEl(id); return els[id]; }
const handlers = {};
const document = {
  getElementById: getEl,
  createElement: (t) => mkEl('<' + t + '>'),
  addEventListener: (ev, fn) => { handlers[ev] = fn; },
  body: mkEl('body'),
  execCommand: () => true,
};

// ---- fetch 打桩 ----
const calls = [];
let deferNext = false, release = null;
const CANNED = {
  'agents.scan': {
    ok: true, count: 4, connected: ['dsh', 'claude-code'], attachable: ['codex'],
    console_risks: [],
    agents: [
      { id: 'dsh', name: 'DSH（DeepSeek Harness）', has_desc: true, detected: true, connected: true,
        evidence: ['目录 ~/.dsh'],
        guard: { available: true, probe: { kind: 'tcp', port: 3080 }, enabled: true, alive: true, starts: 2 },
        status: { plugin_id: 'plugin.dsh', registered: true, running: true, level: 'trust', mcp_kind: 'dsh-plugin', mcp_ok: true } },
      { id: 'claude-code', name: 'Claude Code', has_desc: true, detected: true, connected: true,
        evidence: ['目录 ~/.claude'], hook_kind: 'claude-settings', guard: { available: false },
        status: { plugin_id: 'plugin.claude-code', registered: true, running: true, level: 'write',
                  mcp_kind: 'claude-cli', mcp_ok: false,
                  mcp_hosts: [{ name: 'claude-code-cli', kind: 'claude-cli', exists: true, ok: true },
                              { name: 'claude-desktop', kind: 'mcp-json', config: '%APPDATA%/Claude/claude_desktop_config.json', exists: false, ok: false }] } },
      { id: 'codex', name: 'Codex CLI', has_desc: true, detected: true, connected: false,
        pending_restart: true,
        evidence: ['目录 ~/.codex'], guard: { available: false },
        status: { plugin_id: 'plugin.codex', registered: false, running: false, level: '', mcp_kind: 'codex-toml', mcp_ok: false } },
      { id: 'coze', name: 'Coze（扣子）', has_desc: false, detected: true, connected: false,
        evidence: ['目录 %APPDATA%/Coze'], mcp_kind: 'mcp-json', hook_kind: 'none',
        probe: { config_hint: '没找到本地 MCP 配置文件', note: '桌面版；MCP 在平台上配' } },
    ],
  },
  'agents.add': { ok: true, applied: true, results: [
    { id: 'codex', name: 'Codex CLI', ok: true, steps: [
      { step: '内核侧接口', did: '插入' }, { step: '档位', did: '登记 write' }, { step: 'agent 侧 MCP', did: '写入' }] }] },
  'agents.prompt': (args) => (args && args.id === 'coze')
    ? { ok: true, kind: 'onboard', id: 'coze', name: 'Coze（扣子）', level: '', ops_count: 0,
        prompt: '# 接入手工单：Coze（扣子）（coze）—— 这份给你，接入由你全权负责\n\n一、规矩…\n' }
    : { ok: true, kind: 'attach', id: (args && args.id) || 'dsh', name: 'Codex CLI', level: 'write', ops_count: 32,
        prompt: '# 你是 Codex\n\n## 你手上会多出来一个工具\n- 工具名 brain_ops\n' },
  'agents.verify': { ok: true, id: 'dsh', checks: [
    { name: '接口已登记', ok: true }, { name: '在跑', ok: true },
    { name: 'agent 侧命令不弹黑窗', ok: true, value: 'pythonw' }] },
  'guard.set': { ok: true, id: 'dsh', enabled: false, alive: true },
  'guard.restart': { ok: true, id: 'dsh', killed: [12345], note: '已杀掉；守护会拉回来' },
  'guard.status': { ok: true, interval: 3, rows: [{ id: 'dsh', enabled: true, alive: true, starts: 3 }] },
};
const fetch = (url, opts) => {
  // 面板会直接调内核的热加载口（不是走插件桥）
  if (String(url).indexOf('/api/plugins/reload') >= 0) {
    calls.push({ url, tool: '/api/plugins/reload' });
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true,
      result: { added: ['plugin.codex'], started: ['plugin.codex'], unchanged: ['plugin.dsh'], errors: {} } }) });
  }
  const bodyObj = JSON.parse((opts && opts.body) || '{}');
  calls.push({ url, plugin: bodyObj.plugin, tool: bodyObj.tool, args: bodyObj.args });
  const c = CANNED[bodyObj.tool];
  const data = (typeof c === 'function') ? c(bodyObj.args) : c;
  const resp = { ok: true, json: () => Promise.resolve({ ok: true, data }) };
  if (deferNext) { deferNext = false; return new Promise((res) => { release = () => res(resp); }); }
  return Promise.resolve(resp);
};
const clipboard = [];
const navigator = { clipboard: { writeText: (t) => { clipboard.push(t); return Promise.resolve(); } } };
let confirmMsg = '', confirmAnswer = true;
const window = { confirm: (m) => { confirmMsg = m; return confirmAnswer; } };

const ctx = { document, fetch, navigator, window, console, setTimeout, Promise, JSON, Array, String, Object };
vm.createContext(ctx);
vm.runInContext(code, ctx);

// 假的勾选框（面板用 querySelectorAll 找它们）
function setBoxes(list) {
  // ⚠️ 必须是**同一批对象**：每次新建的话，全选改的是被丢掉的那批，读回来永远还是旧值
  const objs = list.map((b) => ({ value: b[0], checked: !!b[1], disabled: !!b[2] }));
  getEl('agentList').querySelectorAll = () => objs;
  return objs;
}
const guardCalls = (tool) => calls.filter((c) => c.plugin === 'plugin.agent-guard' && c.tool === tool);
const lastScan = () => calls.filter((c) => c.tool === 'agents.scan').length;

(async () => {
  // ⓪ 识别中：状态行和按钮都要转
  getEl('agentScanBtn').textContent = '🔍 自动识别';
  deferNext = true;
  const pending = ctx.agentScan();
  await new Promise((r) => setTimeout(r, 10));
  check('识别中：状态行有转圈齿轮（.spin）+ 文案',
    (getEl('agentState').innerHTML || '').includes('class="spin"') && getEl('agentState').innerHTML.includes('识别中'),
    getEl('agentState').innerHTML);
  check('识别中：按钮自己也转、且点不动',
    (getEl('agentScanBtn').innerHTML || '').includes('class="spin"') && getEl('agentScanBtn').disabled === true, '');
  if (release) release();
  await pending;

  const html = getEl('agentList').innerHTML;
  check('渲染出四行', (html.match(/class="agent-pick"/g) || []).length === 4, html.length);
  check('已连接的显示「🔗 已连接」', (html.match(/🔗 已连接/g) || []).length >= 2, '');
  check('① 默认**全不选**（没有任何 checked）', !/value="[^"]+" checked/.test(html), html.slice(0, 200));
  check('状态行里有「🛡 守护 1：dsh」', (getEl('agentState').textContent || '').includes('🛡 守护 1'), getEl('agentState').textContent);
  // 信箱 2026-10-06 拆掉（在别人电脑上不会有反馈）；提示里不许再出现它
  check('提示里不再有信箱（📮 / data\\onboard）',
    !(getEl('agentNote').textContent || '').includes('📮') &&
    !(getEl('agentNote').textContent || '').includes('onboard'),
    getEl('agentNote').textContent);
  check('提示保留了怎么接（全选 / 生成并接入 / 手工单）',
    (getEl('agentNote').textContent || '').includes('全选') &&
    (getEl('agentNote').textContent || '').includes('生成并接入') &&
    (getEl('agentNote').textContent || '').includes('接入手工单'), getEl('agentNote').textContent);
  check('MCP 那格按面显示：1/2 面（claude-desktop 没装）',
    html.includes('MCP 1/2 面') && html.includes('claude-desktop 没装'), html.match(/MCP [^<]*面[^<]*/));
  check('每个面的状态写在 title 里（鼠标停上去能看到）',
    html.includes('claude-code-cli：在位') && html.includes('claude-desktop：没装'), '');
  check('接不了的（coze）有「📝 接入手工单」按钮，且不显示验收',
    html.includes('📝 接入手工单') && html.includes("agentPrompt('coze')"), '');
  check('能接的（codex）还有「验收」按钮', html.includes("agentVerify('codex')"), '');
  check('config 里有、内核还没读到的行显示「⏳ 待热加载」',
    html.includes('⏳ 待热加载'), html.slice(0, 300));

  // ① 全选：勾上=全勾、去掉=全不勾；半勾=半选
  const boxes = setBoxes([['dsh', false], ['claude-code', false], ['codex', false], ['coze', false, true]]);
  ctx.agentToggleAll(true);
  check('全选：勾上 → 能操作的都勾上（禁用那个不动）',
    boxes.filter((b) => !b.disabled).every((b) => b.checked) && boxes[3].checked === false, JSON.stringify(boxes));
  check('全选：头上那个也勾上', getEl('agentAll').checked === true && getEl('agentAll').indeterminate === false, '');
  boxes[3].checked = true;                 // 禁用的那个就算原来是勾的，也不该被「全不选」带走
  ctx.agentToggleAll(false);
  check('全选：去掉 → 能操作的都不勾（禁用的不掺和）',
    boxes.filter((b) => !b.disabled).every((b) => !b.checked) && boxes[3].checked === true
      && getEl('agentAll').checked === false, JSON.stringify(boxes));
  boxes[3].checked = false;
  boxes[0].checked = true; ctx.agentSyncAll();
  check('勾一半 → 头上是半选（indeterminate）',
    getEl('agentAll').checked === false && getEl('agentAll').indeterminate === true, '');

  // ② 接入：勾 codex → 结果弹窗里应当**并入提示词窗口**
  setBoxes([['dsh', false], ['claude-code', false], ['codex', true], ['coze', true, true]]);
  await ctx.agentAdd();
  const addCall = calls.find((c) => c.tool === 'agents.add');
  check('agentAdd 只把勾上的传下去（含未勾的不传）',
    !!addCall && addCall.args.ids.length === 1 && addCall.args.ids[0] === 'codex', JSON.stringify(addCall && addCall.args));
  const modal = getEl('agentModal').innerHTML || '';
  check('接入成功后自动调内核热加载口（不用重启内核）',
    calls.some((c) => c.tool === '/api/plugins/reload'), JSON.stringify(calls.map((c) => c.tool)));
  check('弹窗里说明"已热加载 + 只剩重启 agent"',
    modal.includes('已热加载') && modal.includes('plugin.codex') && modal.includes('重启对应 agent'),
    modal.slice(0, 400));
  check('接入结果弹窗里有步骤 + **提示词窗口**',
    modal.includes('内核侧接口') && modal.includes('agentPromptBox_codex') && modal.includes('brain_ops'),
    modal.slice(0, 200));
  getEl('agentPromptBox_codex').value = 'PROMPT-FOR-CODEX';
  await ctx.agentCopyPrompt('agentPromptBox_codex', 'agentCopyMsg_codex');
  check('弹窗里那段也能一键复制', clipboard[clipboard.length - 1] === 'PROMPT-FOR-CODEX'
    && (getEl('agentCopyMsg_codex').textContent || '').includes('已复制'), getEl('agentCopyMsg_codex').textContent);

  // ③ 接不了的：点「接入提示词」→ 出的是"接入确认单"
  await ctx.agentPrompt('coze');
  const cHtml = getEl('agentModal').innerHTML || '';
  check('coze 的弹窗标题是「接入手工单」', cHtml.includes('接入手工单'), cHtml.slice(0, 160));
  check('手工单正文进来了（规矩 + 全权负责那套）',
    cHtml.includes('全权负责') && cHtml.includes('规矩'), cHtml.slice(0, 200));

  // ④ 守护开关 / 重启（回归）
  await ctx.agentGuard('dsh', false);
  const setCall = guardCalls('guard.set')[0];
  check('开/关守护 → plugin.agent-guard / guard.set',
    !!setCall && setCall.args.id === 'dsh' && setCall.args.enabled === false, JSON.stringify(setCall && setCall.args));
  confirmMsg = ''; confirmAnswer = true;
  const r0 = guardCalls('guard.restart').length;
  await ctx.agentRestart('dsh');
  check('重启前先确认（会说掐断会话）+ 确认后真调', confirmMsg.includes('掐断') && guardCalls('guard.restart').length === r0 + 1, '');
  check('重启后轮 status 并报"回来了"', (getEl('agentNote').textContent || '').includes('回来了'), getEl('agentNote').textContent);
  confirmAnswer = false;
  const r1 = guardCalls('guard.restart').length;
  await ctx.agentRestart('dsh');
  check('点取消就不重启', guardCalls('guard.restart').length === r1, '');

  // ⑤ 验收 / 关弹窗 / 自动扫描（回归）
  await ctx.agentVerify('dsh');
  check('验收弹窗列出检查（含不弹黑窗那条）',
    (getEl('agentModal').innerHTML || '').includes('agent 侧命令不弹黑窗'), '');
  ctx.agentCloseModal();
  check('关掉后弹窗不再 show 且清空',
    !(getEl('agentModal').className || '').includes('show') && (getEl('agentModal').innerHTML || '') === '', '');
  check('注册了 DOMContentLoaded 处理', typeof handlers.DOMContentLoaded === 'function', '');
  const before = lastScan();
  handlers.DOMContentLoaded();
  await new Promise((r) => setTimeout(r, 30));
  check('打开面板就自动扫一次', lastScan() === before + 1, lastScan() + ' vs ' + before);

  console.log(fails.length ? '\n失败 ' + fails.length + ' 条：' + fails.join('、') : '\n全部通过');
  process.exit(fails.length ? 1 : 0);
})();