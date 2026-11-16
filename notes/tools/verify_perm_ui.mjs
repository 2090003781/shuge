// 控制台权限块「视觉 = 实际」验证：桩 DOM 跑 app.js 里的真实函数 + 真接口。
// ① 保存期间单选被禁用（浏览器不会再派发 change → 不会多发请求）；
// ② 回来之后勾选回填成服务器确认的档位；
// ③ 一次点击 = 一次请求。
import fs from 'node:fs';

const APP = 'D:\\core\\web\\app.js';
const BASE = 'http://127.0.0.1:8790';
const PLUGIN = 'plugin.dsh';
const src = fs.readFileSync(APP, 'utf8');

const start = src.indexOf('// ── 软件操作权限');
const end = src.indexOf('async function savePermissions(');
if (start < 0 || end < 0) throw new Error('定位权限块失败');
const code = src.slice(start, end);

// ---- 桩 DOM ----
function makeEl(id) { return { id, textContent: '', innerHTML: '', style: {} }; }
const els = { permLevels: makeEl('permLevels'), permMsg: makeEl('permMsg'), permOps: makeEl('permOps') };
const radios = [];
function parseRadios() {
  radios.length = 0;
  const re = /<input type="radio" name="permLevel" value="([^"]+)"\s*(checked)?/g;
  let m;
  while ((m = re.exec(els.permLevels.innerHTML))) {
    radios.push({ value: m[1], checked: !!m[2], disabled: false, name: 'permLevel' });
  }
}
const document = {
  getElementById: (id) => els[id] || null,
  querySelectorAll: (sel) => (sel === 'input[name="permLevel"]' ? radios : []),
  querySelector: (sel) => {
    if (sel === 'input[name="permLevel"]:checked') return radios.find((r) => r.checked) || null;
    return null;
  },
};
let reqCount = 0;
const log = [];
async function fetchJSON(url, opts) {
  if (opts && opts.method === 'POST') reqCount++;
  const r = await fetch(BASE + url, opts);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const factory = new Function('document', 'fetchJSON', 'esc', 'log', code + '\nreturn { loadPermissions, renderPermOps, savePermissionsNow, setPermInputsDisabled };');
const api = factory(document, fetchJSON, esc, (m) => log.push(m));

const A = (cond, msg) => { if (!cond) throw new Error('❌ ' + msg); console.log('✅ ' + msg); };

// **先记下原档位**：这个自测会把档位走一遍（write→trust→read），跑完必须还回原样。
// （2026-10-04：原来收尾写死「恢复默认 write」，每跑一次体检就把主人的 trust 按成 write）
const _origPerm = ((await (await fetch(BASE + '/api/permissions?plugin=' + PLUGIN)).json()) || {}).level || 'write';

try {
// 起始状态：先落到 write，保证基线确定
await fetchJSON('/api/permissions/set', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ plugin: PLUGIN, level: 'write' }) });
const before = reqCount;

await api.loadPermissions(PLUGIN);
parseRadios();
A(radios.length === 3, `拿到 3 个档位单选（${radios.map((r) => r.value).join('/')}）`);
A(radios.filter((r) => r.checked).length === 1 && radios.find((r) => r.checked).value === 'write', '初始勾选 = 服务器档位 write');

// 用户点「相信」
radios.forEach((r) => { r.checked = r.value === 'trust'; });
const p = api.savePermissionsNow(PLUGIN);
A(radios.every((r) => r.disabled === true), '请求在途：三个单选全部 disabled（用户点不动 → 不会多发请求）');
await p;
A(radios.every((r) => r.disabled === false), '请求回来：恢复可点');
const srv = await fetchJSON('/api/permissions?plugin=' + PLUGIN);
A(srv.level === 'trust', '服务器实际档位 = trust（真的落库了）');
A(radios.find((r) => r.checked).value === srv.level, '屏幕上的勾选 = 服务器确认的档位');
A(radios.filter((r) => r.checked).length === 1, '有且只有一个勾选');
A(reqCount - before === 1, `一次点击 = 一次写请求（实测 ${reqCount - before} 次）`);

// 恶意/异常情形：在途时被强塞另一个勾选 → 回来必须被服务器值纠正
const before2 = reqCount;
radios.forEach((r) => { r.checked = r.value === 'read'; });   // 在途强塞
const p2 = api.savePermissionsNow(PLUGIN);                     // 这一次提交 read
radios.forEach((r) => { r.checked = r.value === 'trust'; });   // 在途期间再强塞 trust
await p2;
const srv2 = await fetchJSON('/api/permissions?plugin=' + PLUGIN);
A(srv2.level === 'read', '第二个请求落库的是 read');
A(radios.find((r) => r.checked).value === 'read', '在途期间被强塞的勾选被回填纠正 → 视觉 = 实际');
A(radios.every((r) => !r.disabled), '第二次也恢复了可点');
A(reqCount - before2 === 1, '仍然是一次点击一次请求');

} finally {
  // 收尾：**还原成原档位**（不是写死的 write）
  await fetchJSON('/api/permissions/set', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ plugin: PLUGIN, level: _origPerm }) });
  const srv3 = await fetchJSON('/api/permissions?plugin=' + PLUGIN);
  A(srv3.level === _origPerm, `收尾还原为原档位 ${_origPerm}（不是写死的 write）`);
  console.log('\n状态行文案最后一条：', els.permMsg.textContent);
}
