#!/usr/bin/env node
/**
 * DSH 适配插件「注入回执」离线自测（Node，无 DSH 宿主）。
 *
 * 为什么要有它：回执是**跨进程契约**（DSH → 平台账本）。宿主里的代码只有下次重启 DSH 才生效，
 * 而重启本身就换掉了会话——不能靠"重启看看"来验收。这里把插件源码当模块跑，
 * 用桩喂一次会话创建 + 首个用户轮次，验证「注入成功 → platform.context.injected」
 * 与「包过期 → platform.context.inject-missed」两条支路真的会发出去、字段对不对。
 *
 * 用法：node plugins/_dsh_ack_selftest.js
 *       DSH_SB_PLUGIN=<index.js 路径> 可指到别的 profile（默认取 ~/.dsh/profiles/web/...）
 */
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');

const DEFAULT_PLUGIN = path.join(os.homedir(), '.dsh', 'profiles', 'web', 'plugins',
  'dsh-second-brain', 'src', 'index.js');
const PLUGIN = process.env.DSH_SB_PLUGIN || DEFAULT_PLUGIN;

if (!fs.existsSync(PLUGIN)) {
  console.log('SKIP 找不到 DSH 插件源码：' + PLUGIN);
  process.exit(0);
}

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '  ← ' + extra));
  if (!cond) fails.push(name);
}

// ── 把 ESM 源码转成可注入桩的函数体 ────────────────────────────
const src = fs.readFileSync(PLUGIN, 'utf8')
  .replace(/^import[\s\S]*?from\s+'[^']+';$/gm, '')
  .replace(/^export\s+/gm, '')
  .replace(/import\.meta\.url/g, JSON.stringify('file:///fake/dsh-second-brain/src/index.js'));

function buildModule({ sessionStart, config }) {
  const calls = [];
  const stubs = {
    spawn: () => ({ unref() {}, on() {}, pid: 1 }),
    existsSync: () => true,
    mkdirSync: () => {},
    readFileSync: (p) => {
      if (String(p).endsWith('config.json')) return JSON.stringify(config || {});
      if (String(p).endsWith('token.json')) return JSON.stringify({ token: 'tok-test' });
      throw new Error('ENOENT ' + p);
    },
    readdirSync: () => [],
    renameSync: () => {},
    statSync: () => ({ mtimeMs: 1 }),
    dirname: path.dirname,
    join: path.join,
    fileURLToPath: () => path.join(os.homedir(), '.dsh', 'profiles', 'web', 'plugins',
      'dsh-second-brain', 'src', 'index.js'),
    createUserMessage: (m) => ({ ...m, source: { kind: 'plugin', plugin: 'dsh-second-brain' } }),
    fetch: async (url, init) => {
      calls.push({ url: String(url), body: init && init.body ? JSON.parse(init.body) : null,
        headers: (init && init.headers) || {} });
      let out = {};
      if (String(url).endsWith('/api/agent/session-start')) out = sessionStart;
      return { ok: true, status: 200, json: async () => out };
    },
    console: { log: () => {}, error: () => {} },
    setTimeout, clearTimeout, AbortSignal, Date, JSON, Promise,
  };
  const factory = new Function('stubs', `with (stubs) {\n${src}\n;return { apply, name, pending }; }`);
  return { mod: factory(stubs), calls };
}

// 桩出一轮：会话创建（拿包）→ 首个用户轮次（注入）
async function drive(sessionStart, config) {
  const { mod, calls } = buildModule({ sessionStart, config });
  const handlers = {};
  mod.apply({ on: (ev, fn) => { handlers[ev] = fn; } });
  handlers['session/created']({ id: 'testsession' });
  await mod.pending.ready;
  // 真实驱动里 decision.messages 就是本轮批次（含用户消息）；hasUserTurn 判的是它
  const decision = { kind: 'enter', messages: [{ source: { kind: 'user' } }] };
  const out = await handlers['agent/pre-step'](
    { messages: [{ source: { kind: 'user' } }], signal: undefined },
    async () => decision,
  );
  return { calls, out, mod, handlers };
}

function sent(calls, type) {
  return calls.filter(c => c.url.endsWith('/api/bridge/send') && c.body
    && c.body.event && c.body.event.type === type).map(c => c.body);
}

(async () => {
  // ── 支路 1：拿到包 + 首轮注入 → 必须有 platform.context.injected ──
  const ok = await drive({ injected: true, content: 'PACK-CONTENT', bytes: 12, reason: 'ok' });
  check('拿到包并注入（批次追加了插件消息）', ok.out.messages.length === 2,
    'messages=' + ok.out.messages.length);
  const injected = sent(ok.calls, 'platform.context.injected');
  check('发了 platform.context.injected', injected.length === 1, 'count=' + injected.length);
  if (injected.length) {
    const p = injected[0].event.payload;
    check('回执带会话号（dsh- 前缀）', p.sessionId === 'dsh-testsession', JSON.stringify(p));
    check('回执带字节数', p.bytes === 12, JSON.stringify(p));
    check('回执带字符数', p.chars === 'PACK-CONTENT'.length, JSON.stringify(p));
    check('回执带时延字段', typeof p.latencyMs === 'number', JSON.stringify(p));
    check('来源是 agent.dsh', injected[0].event.source === 'agent.dsh', injected[0].event.source);
    check('agentId 是 agent.dsh', injected[0].agentId === 'agent.dsh', injected[0].agentId);
    const call0 = ok.calls.find(c => c.url.endsWith('/api/bridge/send')
      && c.body && c.body.event && c.body.event.type === 'platform.context.injected');
    check('带插件面 token 头', !!call0 && call0.headers['X-Plugin-Token'] === 'tok-test',
      JSON.stringify(call0 && call0.headers));
  }
  check('注入成功时不发 miss 事件', sent(ok.calls, 'platform.context.inject-missed').length === 0);

  // ── 支路 2：包过期（TTL=1ms + 等一会）→ 必须发 inject-missed，且不注入 ──
  const { mod, calls } = buildModule({
    sessionStart: { injected: true, content: 'PACK-CONTENT', bytes: 12, reason: 'ok' },
    config: { packTtlMs: 1 },
  });
  const handlers = {};
  mod.apply({ on: (ev, fn) => { handlers[ev] = fn; } });
  handlers['session/created']({ id: 'testsession2' });
  await mod.pending.ready;
  await new Promise(r => setTimeout(r, 30));   // 让包过期
  const out2 = await handlers['agent/pre-step'](
    { messages: [{ source: { kind: 'user' } }], signal: undefined },
    async () => ({ kind: 'enter', messages: [{ source: { kind: 'user' } }] }),
  );
  check('过期包不注入（批次原样）', out2.messages.length === 1, 'messages=' + out2.messages.length);
  const missed = sent(calls, 'platform.context.inject-missed');
  check('发了 platform.context.inject-missed', missed.length === 1, 'count=' + missed.length);
  if (missed.length) {
    check('miss 原因为 expired', missed[0].event.payload.reason === 'expired',
      JSON.stringify(missed[0].event.payload));
    check('miss 带会话号', missed[0].event.payload.sessionId === 'dsh-testsession2',
      JSON.stringify(missed[0].event.payload));
  }
  check('过期时不发 injected', sent(calls, 'platform.context.injected').length === 0);

  // ── 支路 3：平台说没注入（如开关关闭）→ 两条回执都不该发（平台自己已记账）──
  const noinj = await drive({ injected: false, reason: 'disabled' });
  check('未注入时无 injected 回执', sent(noinj.calls, 'platform.context.injected').length === 0);
  check('未注入时无 miss 回执', sent(noinj.calls, 'platform.context.inject-missed').length === 0);
  check('未注入时不追加消息', noinj.out.messages.length === 1, 'messages=' + noinj.out.messages.length);

  console.log('\n结果：' + (fails.length ? fails.length + ' 项失败 → ' + fails.join(' / ') : '全部通过'));
  process.exit(fails.length ? 1 : 0);
})();


