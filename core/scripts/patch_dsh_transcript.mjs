// 给 DSH 插件 dsh-transcript 打**一刀**补丁：加第四路（上报新平台）+ 关掉旧的 49520。
//
//   node scripts/patch_dsh_transcript.mjs           # 打（幂等）
//   node scripts/patch_dsh_transcript.mjs --check    # 只查
//
// ── 历史（2026-10-03 两次踩坑，都记在这儿）─────────────────────────────
// 坑①  我先分两刀打（① 加第四路 ② 关旧路）。第二刀把 `send()` 改成
//      `if (LEGACY_WRITER_ENABLED) sendLegacy(evt)` 时，**第一刀插进 `send()` 的那几行被裹进了
//      `sendLegacy`** → 旧路开关是 false ⇒ **第四路根本不执行**（插件照常加载、钩子照常注册、
//      什么都不报错，但平台一条都收不到）。教训：改同一个函数的两个补丁必须合成一刀。
// 坑②  加"同一时刻只允许一发在飞"时，我把守卫放在 `sendPlatform()` 里，却**漏了 `outboxFlush()`
//      里的 post** —— 每个事件又补送最多 3 条，把守卫绕过去了。模拟台量出 **burst 在飞峰值 201**。
//      教训：**出口只能有一个**，所有上报都得从同一个泵出去。
//
// ── 这一刀的上传路径（一个出口）──────────────────────────────
//   send(evt) → 入内存队列 → pump() 抽一条发；在飞就自然排队
//   · 成功：出队；队列空了就把落盘残留清掉
//   · 失败：**落盘**（outbox）并从内存出队 → 下次由 pump 从盘上捞回来（不重复）
//   · 队列空时才去读盘（正常工作时**零磁盘 I/O**）
//   · 上报的 AbortController 定时器 **unref()** —— 绝不让事件循环"有事没干完"
//     （辞玖 2026-10-03 11:30「模型无法作答」最可能就是它：当时版本 1200ms 且**没有** unref，
//      事件密的时候每个都留一个 ref'd 定时器 ⇒ 回合结束不了。模拟台 `refdtimer` 那一格量了对照。）
//
// 旧路（TCP 49520 → reasonix supervisor 的 writer → vault/日志/reasonix-raw/）
// 用 `LEGACY_WRITER_ENABLED = false` 关掉（代码留着，一行回滚）。
import fs from 'node:fs';
import path from 'node:path';

// 落点从**当前用户**推（原来写死了用户名，2026-10-06 去掉）：DSH_HOME 可覆盖
const HOME = process.env.DSH_HOME || (process.env.USERPROFILE || process.env.HOME || '').replace(/\\/g, '/');
const TARGETS = [
  HOME + '/.dsh/profiles/web/plugins/dsh-transcript/src/index.js',
  HOME + '/.dsh/profiles/headless/plugins/dsh-transcript/src/index.js',
];

const OLD_HEAD = `// 设计：转录是旁路，任何失败静默丢弃，绝不阻断 dsh 主流程。`;
const NEW_HEAD = `// 设计：转录是旁路，任何失败静默丢弃，绝不阻断 dsh 主流程。
//
// **第四路（2026-10-03）**：同一条事件**同时**上报给新平台（枢阁）——
//   POST http://127.0.0.1:8790/api/bridge/send {agentId: agent.dsh, call.tool: agent.record.dsh}
//   → 内核按能力名路由到 agent.dsh 自己的接口 → 落 data/transcripts/agent.dsh/<日期>.jsonl
//   → 独立插件 plugin.transcript 组装成"一天一份"写进库（vault/日志/agent.dsh-raw/<日期>/会话.md）。
// 为什么加：旧 writer 把助手正文砍到 100 字节、用户 prompt 砍到 150 字节**且不留痕**；
//   新链路送**事件原文**，不截断。
// **旧路已关**（\`LEGACY_WRITER_ENABLED = false\`）。
// **绝不拖累正常回复**（辞玖明确要求）：一个出口（pump）+ 同一时刻只一发在飞 +
//   **每次只抽 3 发**（超出的落盘）+ 上报定时器 **unref()** + 失败落盘下次补送。
//   这三条是模拟台量出来的：全并发峰值 201；全串行虽然峰值 1 但占住循环 42 秒；
//   而"没 unref 的 1200ms 定时器"实测每次事件多占 1231ms —— 那就是 2026-10-03 11:30 卡住的机理。`;

const OLD_ANCHOR = `// ── 转发（短连接，失败静默丢弃，但连续失败会告警）────────────
function send(evt) {`;

const NEW_ANCHOR = `// ── 第四路：上报新平台（枢阁）────────────────────────────
const CORE = 'http://127.0.0.1:8790';
const RECORD_TOOL = 'agent.record.dsh';          // 能力名==工具名；agent.dsh 自己的日志记录入口
const TOKEN_FILE = 'D:\\core\\data\\plugins\\token.json';
const RECORD_TIMEOUT_MS = 800;                   // 短一点：宁可下次补送，也别占着事件循环
const OUTBOX = 'D:\\core\\data\\transcripts\\.dsh-outbox.jsonl';
const OUTBOX_WARN_THRESHOLD = 5;

// 旧路开关：退役 supervisor 后置 false（代码留着，一行回滚）。它**只关旧 TCP**，第四路不受影响。
const LEGACY_WRITER_ENABLED = false;

let _token = '';
let _inFlight = false;
let _streak = 0;
let _warned = false;
const _queue = [];                                // 内存队列（正常工作时零磁盘 I/O）

function pluginToken() {
  if (_token) return _token;
  try { _token = String(JSON.parse(fs.readFileSync(TOKEN_FILE, 'utf8')).token || ''); } catch { /* 没 token 也试一次 */ }
  return _token;
}

function recordBody(evt) {
  return JSON.stringify({
    id: 'dsh-transcript-' + Date.now(),
    agentId: 'agent.dsh',
    kind: 'tool-call',
    call: {
      tool: RECORD_TOOL,
      args: {
        event: {
          type: evt.event,
          source: 'dsh',
          time: evt.ts,
          payload: { session: evt.sessionId, raw: evt },   // ← 原文（完整内容）
        },
      },
    },
  });
}

function post(evt) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), RECORD_TIMEOUT_MS);
  // ★★ 命门：unref —— 这个定时器绝不能成为"事件循环还没干完的事"，
  //    否则事件一密，dsh 的回合就迟迟结束不了（2026-10-03 11:30 就是这么卡住的）。
  try { if (typeof timer.unref === 'function') timer.unref(); } catch { /* 没 unref 就算了 */ }
  return fetch(CORE + '/api/bridge/send', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Plugin-Token': pluginToken() },
    body: recordBody(evt),
    signal: ac.signal,
  }).then(async (r) => {
    const j = await r.json().catch(() => ({}));
    return !!(j && j.result && j.result.ok);
  }).finally(() => clearTimeout(timer));
}

// 落盘（只在失败/积压时用）
function persist(evt) {
  try {
    fs.mkdirSync(path.dirname(OUTBOX), { recursive: true });
    fs.appendFileSync(OUTBOX, JSON.stringify(evt) + '\\n', 'utf8');
  } catch { /* 旁路 */ }
}

function clearOutbox() {
  try { if (fs.existsSync(OUTBOX)) fs.unlinkSync(OUTBOX); } catch { /* 旁路 */ }
}

// 从盘上捞一条（**只在内存队列空的时候**才会走到这儿；捞到就从盘上删掉，不会重复）
function takeFromOutbox() {
  try {
    if (!fs.existsSync(OUTBOX)) return null;
    const lines = fs.readFileSync(OUTBOX, 'utf8').split('\\n').filter(Boolean);
    if (!lines.length) { clearOutbox(); return null; }
    const first = lines.shift();
    if (lines.length) fs.writeFileSync(OUTBOX, lines.join('\\n') + '\\n', 'utf8');
    else clearOutbox();
    try { return JSON.parse(first); } catch { return takeFromOutbox(); }
  } catch { return null; }
}

function bump(running) {
  if (running) { _streak = 0; return; }
  _streak += 1;
  if (!_warned && _streak >= OUTBOX_WARN_THRESHOLD) {
    _warned = true;
    try {
      process.stderr.write('[dsh-transcript] 上报平台连续失败 ' + _streak +
        ' 次（' + CORE + '）——事件已落 outbox，下次事件会自动补送：' + OUTBOX);
    } catch { /* 旁路 */ }
  }
}

// ★ 唯一出口。为什么带"预算"（2026-10-03 模拟台量的）：
//   · 全并发（原始写法）：200 条 → 在飞峰值 201，循环占 ~1-3 秒
//   · 全串行（上一版）：  200 条 → 峰值 1 ✅ 但**占住循环 42 秒** ❌ —— 比全并发还糟
//   · 每次只抽 PUMP_BUDGET 发、超出的落盘：峰值 1，循环占 ~0.5 秒，
//     剩下的事件在盘上（不丢），由**后续事件**继续抽 —— 事件流本身就是排空的节拍。
const PUMP_BUDGET = 3;

function pump(budget = PUMP_BUDGET) {
  if (_inFlight || budget <= 0) {
    // 预算用完 / 有在飞的 → 把内存里剩下的落盘，别让它们拖着循环，也别丢
    if (!_inFlight && budget <= 0 && _queue.length) {
      for (const e of _queue.splice(0)) persist(e);
    }
    return;
  }
  const evt = _queue.length ? _queue.shift() : takeFromOutbox();
  if (!evt) return;
  _inFlight = true;
  post(evt)
    .then((ok) => {
      _inFlight = false;
      bump(ok);
      if (ok) pump(budget - 1);                // 成功：在预算内继续抽
      else persist(evt);                       // 失败：落盘，稍后由 takeFromOutbox 捞回来
    })
    .catch(() => { _inFlight = false; bump(false); persist(evt); });
}

// ── 转发 ────────────────────────────────────────────────────
function send(evt) {
  try { _queue.push(evt); pump(); } catch { /* 旁路 */ }
  if (LEGACY_WRITER_ENABLED) sendLegacy(evt);   // 旧路：默认关
}

function sendLegacy(evt) {`;

export function patchFile(file) {
  let s = fs.readFileSync(file, 'utf8');
  if (s.includes('第四路：上报新平台')) return 'already';
  if (s.includes('LEGACY_WRITER_ENABLED')) {
    throw new Error('这文件是老版本（两刀版），先从 .bak-20261003-before-uplink 恢复再打：' + file);
  }
  if (!s.includes(OLD_ANCHOR)) throw new Error('锚点丢了（send）：' + file);
  if (!s.includes(OLD_HEAD)) throw new Error('锚点丢了（设计注释）：' + file);
  s = s.replace(OLD_HEAD, NEW_HEAD).replace(OLD_ANCHOR, NEW_ANCHOR);
  fs.writeFileSync(file, s, 'utf8');
  return 'patched';
}

export function checkFile(file) {
  try {
    const s = fs.readFileSync(file, 'utf8');
    return s.includes('第四路：上报新平台') &&
           s.includes('if (LEGACY_WRITER_ENABLED) sendLegacy(evt);') &&
           s.includes('timer.unref()') &&
           s.includes('function pump()') &&
           !s.includes('function sendPlatform');
  } catch {
    return false;
  }
}

if (process.argv[1] && process.argv[1].endsWith('patch_dsh_transcript.mjs')) {
  const checkOnly = process.argv.includes('--check');
  let bad = 0;
  for (const f of TARGETS) {
    if (!fs.existsSync(f)) { console.log('  ⏭ 没有这个文件：' + f); continue; }
    if (checkOnly) {
      const done = checkFile(f);
      if (!done) bad += 1;
      console.log((done ? '  ✅ 已打（一个出口版）' : '  ❌ 没打 / 是旧版本') + '：' + f);
    } else {
      try {
        const r = patchFile(f);
        console.log('  ' + (r === 'patched' ? '✅ 已打（一个出口版）' : '⏭ 早就打过了') + '：' + f);
      } catch (e) {
        bad += 1;
        console.log('  ❌ ' + f + ' → ' + e.message);
      }
    }
  }
  process.exit(bad ? 1 : 0);
}
