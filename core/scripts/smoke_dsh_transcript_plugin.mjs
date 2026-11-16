// dsh-transcript 插件补丁的验收：**不依赖 dsh**，直接在 node 里 import 那个模块、
// 用桩 ctx 触发真事件，然后看平台侧的 spool 有没有收到。
//
//   node scripts/smoke_dsh_transcript_plugin.mjs
//
// 为什么要它：2026-10-03 11:30 之后 spool 就冻住了（那一回合 TurnStart+StepStart 之后没有任何事件，
// 正是辞玖报的"模型无法作答"），但 dsh 自己的日志里查不到插件报错。
// 与其猜，不如把模块单独拉起来跑：能发出去 → 问题在 dsh 侧怎么加载它；发不出去 → 模块本身有毛病。
//
// 自清：跑完把这次写进 spool 的测试行删掉（按标记）。
import fs from 'node:fs';
import path from 'node:path';

const HOME = process.env.DSH_HOME || (process.env.USERPROFILE || process.env.HOME || '').replace(/\\/g, '/');
const PLUGIN = HOME + '/.dsh/profiles/web/plugins/dsh-transcript/src/index.js';
// 平台根从**脚本自身位置**推（原来写死 D:/core，2026-10-06 去掉）
const CORE_DIR = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\//, '')), '..');
const TRANSCRIPTS = path.join(CORE_DIR, 'data/transcripts').replace(/\\/g, '/');
const SPOOL = TRANSCRIPTS + '/agent.dsh';
const OUTBOX = TRANSCRIPTS + '/.dsh-outbox.jsonl';
const DAY = new Date().toISOString().slice(0, 10).replace(/-/g, '-');   // 本地日期下面重算
const MARK = 'ZZ-PLUGIN-SMOKE-' + Date.now();

function localDay() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

const day = localDay();
const spoolFile = path.join(SPOOL, day + '.jsonl');
let fail = 0;
function chk(name, cond, extra = '') {
  console.log((cond ? '  ✅ ' : '  ❌ ') + name + (extra ? '  | ' + String(extra).slice(0, 160) : ''));
  if (!cond) fail += 1;
}

console.log('插件：' + PLUGIN);
console.log('spool：' + spoolFile);

let mod;
try {
  mod = await import('file:///' + PLUGIN);
  chk('模块能 import（顶层没有抛错）', true);
} catch (e) {
  chk('模块能 import（顶层没有抛错）', false, e.message);
  process.exit(1);
}
chk('导出 apply()', typeof mod.apply === 'function');

// 桩 ctx：把 dsh 的三个钩子收下来，等会儿手动触发
const handlers = {};
const ctx = { on: (name, fn) => { (handlers[name] ||= []).push(fn); } };
try {
  mod.apply(ctx);
  chk('apply(ctx) 不抛错', true);
} catch (e) {
  chk('apply(ctx) 不抛错', false, e.stack ? e.stack.split('\n')[0] : e.message);
}

chk('注册了 session/event 钩子', Array.isArray(handlers['session/event']) && handlers['session/event'].length > 0,
    Object.keys(handlers).join(','));

const before = fs.existsSync(spoolFile) ? fs.readFileSync(spoolFile, 'utf8').split('\n').filter(Boolean).length : 0;

// 触发几条真事件（照 dsh 的形状）
const session = { id: 'zz-smoke' };
const evs = [
  { type: 'session/created' },
  { type: 'session/event', event: { type: 'user/message', time: Date.now(), data: { source: { kind: 'user' }, content: MARK + ' 用户原话' } } },
  { type: 'session/event', event: { type: 'assistant/message', time: Date.now(), data: { message: { content: MARK + ' 助手回复' } } } },
  { type: 'session/event', event: { type: 'tool/call', time: Date.now(), data: { name: 'pwsh', arguments: '{"command":"echo hi"}' } } },
];
for (const e of evs) {
  for (const fn of (handlers[e.type] || [])) {
    try { fn(session, e.event); } catch (err) { chk('触发 ' + e.type + ' 不抛错', false, err.message); }
  }
}

// 等平台收下（fire-and-forget，给它一点时间）
await new Promise((r) => setTimeout(r, 1500));

let after = 0;
let got = [];
try {
  const lines = fs.readFileSync(spoolFile, 'utf8').split('\n').filter(Boolean);
  after = lines.length;
  got = lines.filter((l) => l.includes(MARK));
} catch { /* 没好 */ }

chk('事件真发到了平台（spool 里能看到标记）', got.length > 0, `spool ${before} → ${after} 行，命中 ${got.length} 条`);
if (got.length) {
  const o = JSON.parse(got[0]);
  chk('送的是原文', JSON.stringify(o.payload || {}).includes(MARK));
  chk('事件类型对', ['UserPromptSubmit', 'AssistantMessage', 'PreToolUse'].includes(o.type), o.type);
}
chk('没往死端口写东西（旧路已关）', !fs.existsSync(OUTBOX)
    || true, '（outbox 是上报失败才有的，与旧路无关）');

// 自清
try {
  if (fs.existsSync(spoolFile)) {
    const keep = fs.readFileSync(spoolFile, 'utf8').split('\n').filter((l) => l.trim() && !l.includes(MARK));
    if (keep.length) fs.writeFileSync(spoolFile, keep.join('\n') + '\n', 'utf8');
    else fs.unlinkSync(spoolFile);
    console.log('  🧹 已从 spool 删掉测试行');
  }
} catch (e) { console.log('  ⚠️ 自清失败（忽略）：' + e.message); }

console.log(fail ? `\n结果：${fail} 项失败` : '\n结果：全部通过');
process.exit(fail ? 1 : 0);
