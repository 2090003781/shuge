// 退役 DSH 侧那半条推送链路（辞玖 2026-10-04）。
//
// 为什么退：这个文件住在 **DSH 进程里**，内核监督不到它。2026-10-03 12:30 它静默停了
// 11 个小时 —— outbox 里只剩一条测试事件，`.state.json` 还在每 30 秒报 ok:true，
// **没人知道转录断了**。改成内核侧拉取（plugin.dsh-pull 读 ~/.dsh/sessions/**/session.v3.jsonl.zstd），
// 那个进程写在 config.yaml 的 plugins 里，由内核 Supervisor 管重启，还有看门狗会写告警。
//
// 本补丁只做一件事：加总开关 `PUSH_ENABLED = false` 并让 `send()` 首行就返回。
// 旧代码一个字节没删 —— 回滚只需把它改成 true，**并同时停掉内核里的 plugin.dsh-pull**
// （两边同时开 = 同一天两份 spool 串味）。
//
//   node scripts/patch_dsh_transcript_retire.mjs
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

const SWITCH = `// ── 总开关（2026-10-04）：推的那半条链路**退役**，改由内核侧拉取 ──────────────
//    内核里的 plugin.dsh-pull 直接读 ~/.dsh/sessions/**/session.v3.jsonl.zstd。
//    为什么：这个文件住在 DSH 进程里，内核监督不到它 —— 2026-10-03 12:30 它静默停了
//    11 个小时，outbox 里只剩一条测试事件，谁也不知道。
//    回滚：改成 true **并**停掉内核的 plugin.dsh-pull（两边同开 = 同一天两份串味）。
const PUSH_ENABLED = false;

function send(evt) {
  if (!PUSH_ENABLED) return;                     // 退役：一个字都不往外发
  try { _queue.push(evt); pump(); } catch { /* 旁路 */ }
  if (LEGACY_WRITER_ENABLED) sendLegacy(evt);   // 旧路：默认关
}`;

const OLD = `function send(evt) {
  try { _queue.push(evt); pump(); } catch { /* 旁路 */ }
  if (LEGACY_WRITER_ENABLED) sendLegacy(evt);   // 旧路：默认关
}`;

let n = 0;
for (const prof of ['web', 'headless']) {
  const p = path.join(os.homedir(), '.dsh', 'profiles', prof, 'plugins', 'dsh-transcript', 'src', 'index.js');
  if (!fs.existsSync(p)) {
    console.log(`  跳过（没有）：${p}`);
    continue;
  }
  let s = fs.readFileSync(p, 'utf8');
  if (s.includes('const PUSH_ENABLED = false;')) {
    console.log(`  已经是退役态：${p}`);
    continue;
  }
  if (!s.includes(OLD)) {
    console.log(`  ❌ 找不到 send() 锚点，没改：${p}`);
    process.exitCode = 1;
    continue;
  }
  const bak = p + '.bak-retire-push';
  if (!fs.existsSync(bak)) fs.copyFileSync(p, bak);
  fs.writeFileSync(p, s.replace(OLD, SWITCH), 'utf8');
  console.log(`  ✓ 已退役推送：${p}（备份 ${path.basename(bak)}）`);
  n++;
}
console.log(n ? `改好 ${n} 个` : '没有需要改的');
