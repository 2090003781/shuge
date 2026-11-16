// dsh-transcript 插件的**模拟台**：在没有 dsh 的情况下把模块拉起来跑各种情况。
//
//   node scripts/sim_dsh_transcript.mjs              # 跑全部情形（每格起一个子进程）
//   node scripts/sim_dsh_transcript.mjs --case hang   # 只跑某一格（子进程用）
//
// **量什么**：每个情形起一个**子进程**，让它自然退出，量"从起进程到退出"的墙钟。
// 为什么用这个当判据：dsh 的回合结束不了 = Node 事件循环一直"有事没干完"；
// 而事件循环空不空，最直接的体现就是**进程能不能自然退出**。
// 基准是 noop 那一格（只起进程、不加载模块）—— 各格减去它，就是"这个功能多占住的毫秒"。
//
// 情形：
//   noop    基准（只起进程）
//   normal  平台正常，5 条事件       → 事件进 spool 吗？
//   hang    平台挂住（fetch 永不 settle）→ 会不会把循环拖住？事件丢不丢？
//   refuse  平台立刻拒连             → 对照 hang
//   burst   200 条并发               → 同一时刻在飞的请求峰值（辞玖要求 ≤1）
//   drain   先失败后恢复             → 落进 outbox 的能不能补送出去
//   legacy  **改动前的模块**（旧路开着、写 49520 那个死端口）→ 复现 11:30 卡住的机理
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const SELF = fileURLToPath(import.meta.url);
const HOME = process.env.DSH_HOME || (process.env.USERPROFILE || process.env.HOME || '').replace(/\\/g, '/');
const TMP = process.env.TEMP || (HOME + '/AppData/Local/Temp');
const PATCHED = HOME + '/.dsh/profiles/web/plugins/dsh-transcript/src/index.js';
const PRISTINE = PATCHED + '.bak-20261003-before-uplink';
const LEGACY_MJS = TMP.replace(/\\/g, '/') + '/dsh_transcript_legacy_module.mjs';
// 平台根从**脚本自身位置**推（原来写死 D:/core，2026-10-06 去掉）
const CORE_DIR = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\//, '')), '..');
const TRANSCRIPTS = path.join(CORE_DIR, 'data/transcripts').replace(/\\/g, '/');
const SPOOL = TRANSCRIPTS + '/agent.dsh';
const OUTBOX = TRANSCRIPTS + '/.dsh-outbox.jsonl';
const MARK = 'ZZ-SIM-' + process.pid;

const day = (() => { const d = new Date(); const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`; })();
const spoolFile = path.join(SPOOL, day + '.jsonl');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function countLines(p) {
  try { return fs.readFileSync(p, 'utf8').split('\n').filter(Boolean).length; } catch { return 0; }
}

// ══════════ 子进程：跑一格 ══════════
async function runCase(name) {
  if (name === 'legacy') {
    if (!fs.existsSync(PRISTINE)) { console.log('SKIP 没有改动前的备份'); return; }
    fs.copyFileSync(PRISTINE, LEGACY_MJS);          // .bak 扩展名 node 不认，先挪成 .mjs
  }
  const target = name === 'legacy' ? LEGACY_MJS : PATCHED;
  if (name === 'noop') return;                      // 连模块都不加载（纯基准）

  // 对照格：5 个 1200ms 定时器，一个不 unref、一个 unref。
  // 用来判"11:30 卡住是不是我那个没 unref 的 setTimeout 造成的"（纯计时器行为，不涉及模块）。
  if (name === 'refdtimer' || name === 'unrefdtimer') {
    for (let i = 0; i < 5; i += 1) {
      const t = setTimeout(() => {}, 1200);
      if (name === 'unrefdtimer' && typeof t.unref === 'function') t.unref();
    }
    return;                                         // 什么都不做，让进程自然退出
  }

  const realFetch = globalThis.fetch;
  let concur = 0, peak = 0;
  const counting = (u, o) => {
    concur += 1; if (concur > peak) peak = concur;
    return realFetch(u, o).finally(() => { concur -= 1; });
  };
  if (name === 'normal' || name === 'burst' || name === 'drain') globalThis.fetch = counting;
  if (name === 'hang') globalThis.fetch = () => new Promise(() => {});
  if (name === 'refuse') globalThis.fetch = () => Promise.reject(new Error('ECONNREFUSED'));

  const mod = await import('file:///' + target + '?t=' + Date.now());
  const handlers = {};
  mod.apply({ on: (n, f) => { (handlers[n] ||= []).push(f); } });
  const session = { id: 'zz-sim-' + process.pid };
  const fire = (i, content) => {
    for (const fn of (handlers['session/event'] || [])) {
      fn(session, { type: 'assistant/message', time: Date.now(),
                    data: { message: { content: `${MARK} ${content} #${i}` } } });
    }
  };

  const n = name === 'burst' ? 200 : 5;
  for (let i = 0; i < n; i += 1) fire(i, name);

  if (name === 'drain') {
    await sleep(500);                               // 让失败都落进 outbox
    globalThis.fetch = counting;                    // 平台恢复
    fire(999, 'recover');                           // 触发一次 outboxFlush
    await sleep(900);
  }
  console.log(`PEAK ${peak} MARK ${MARK}`);
  // 到这里什么都不做 —— 让进程**自然退出**，父进程量的就是"循环什么时候空"
}

// ══════════ 父进程：逐个跑、量墙钟、算增量 ══════════
if (process.argv.includes('--case')) {
  const i = process.argv.indexOf('--case');
  await runCase(process.argv[i + 1]);
} else {
  const CASES = [
    ['noop', '基准：只起进程'],
    ['normal', '平台正常，5 条'],
    ['hang', '平台挂住（fetch 永不返回）'],
    ['refuse', '平台立刻拒连'],
    ['burst', '200 条并发'],
    ['drain', '先失败后恢复（含固定 900ms 等待）'],
    ['legacy', '对照：改动前的模块（旧路还开着）'],
    ['refdtimer', '对照：5 个 1200ms ref 定时器（= 改前的上报超时）'],
    ['unrefdtimer', '对照：同样 5 个定时器但 unref'],
  ];
  const run = (name) => new Promise((res) => {
    const t0 = Date.now();
    const ch = spawn(process.execPath, [SELF, '--case', name], { stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '';
    ch.stdout.on('data', (d) => { out += d.toString(); });
    ch.stderr.on('data', (d) => { out += d.toString(); });
    ch.on('exit', () => res({ ms: Date.now() - t0, out: out.trim() }));
  });

  console.log('模拟台：' + PATCHED);
  console.log(`（基线：spool ${countLines(spoolFile)} 行 / outbox ${countLines(OUTBOX)} 行）\n`);
  console.log('  情形      自然退出      相对基准     spoolΔ  outboxΔ   备注');

  const results = [];
  let floor = null;
  for (const [name, note] of CASES) {
    const b = { spool: countLines(spoolFile), outbox: countLines(OUTBOX) };
    const r = await run(name);
    const a = { spool: countLines(spoolFile), outbox: countLines(OUTBOX) };
    if (name === 'noop') floor = r.ms;
    if (r.out.includes('SKIP')) { console.log(`  ${name.padEnd(8)} ⏭ ${note}`); continue; }
    const peak = /PEAK (\d+)/.exec(r.out);
    const extra = floor === null ? '' : `${r.ms - floor >= 0 ? '+' : ''}${r.ms - floor}ms`;
    console.log(`  ${name.padEnd(8)} ${String(r.ms + 'ms').padEnd(12)} ${extra.padEnd(11)} `
      + `${String(a.spool - b.spool).padEnd(7)} ${String(a.outbox - b.outbox).padEnd(9)} `
      + `${peak ? '在飞峰值 ' + peak[1] + '  ' : ''}${note}`);
    results.push({ name, ms: r.ms, extra: floor === null ? null : r.ms - floor,
                   ds: a.spool - b.spool, doo: a.outbox - b.outbox, peak: peak ? +peak[1] : null });
  }

  // 自清
  for (const f of [spoolFile, OUTBOX]) {
    try {
      if (!fs.existsSync(f)) continue;
      const all = fs.readFileSync(f, 'utf8').split('\n').filter((l) => l.trim());
      const keep = all.filter((l) => !l.includes('ZZ-SIM-'));
      if (keep.length) fs.writeFileSync(f, keep.join('\n') + '\n', 'utf8');
      else fs.unlinkSync(f);
    } catch { /* 忽略 */ }
  }
  try { fs.unlinkSync(LEGACY_MJS); } catch { /* 忽略 */ }

  // ── 判据 ──────────────────────────────────────────────
  const g = (n) => results.find((r) => r.name === n);
  const bad = [];
  if (!g('normal') || g('normal').ds < 1) bad.push('normal：事件没进 spool');
  if (g('burst') && g('burst').peak !== null && g('burst').peak > 1) bad.push(`burst：在飞峰值 ${g('burst').peak} > 1`);
  for (const n of ['normal', 'hang', 'refuse']) {
    const r = g(n);
    if (r && r.extra !== null && r.extra > 1200) bad.push(`${n}：比基准多占 ${r.extra}ms（>1200ms，会拖住回合）`);
  }
  const lg = g('legacy');
  console.log('\n判据：');
  console.log('  · normal 事件进 spool：' + (g('normal') && g('normal').ds >= 1 ? '✅' : '❌'));
  console.log('  · burst 在飞峰值 ≤1：' + (g('burst') && g('burst').peak <= 1 ? '✅ ' + g('burst').peak : '❌'));
  console.log('  · normal/hang/refuse 相对基准多占 ≤1200ms：'
    + (['normal', 'hang', 'refuse'].every((n) => !g(n) || g(n).extra <= 1200) ? '✅' : '❌'));
  if (lg) console.log(`  · 对照 legacy（旧路开着）多占 ${lg.extra}ms —— 这个数大，就说明"死端口悬挂 socket 会拖住回合"`);
  console.log(bad.length ? '\n结果：❌ ' + bad.join(' / ') : '\n结果：✅ 符合判据（这个功能不会拖住回合）');
  process.exit(bad.length ? 1 : 0);
}
