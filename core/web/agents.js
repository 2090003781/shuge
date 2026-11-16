// Agent 接入面板（控制台置顶）—— 自动识别本机 agent → 勾选 → 生成接入 → 弹窗给可复制的提示词
// 辞玖 2026-10-05：「自动识别 → 勾选确认即生成 + 一段可复制的提示词（弹窗）」
//   「① 不要有任何黑窗口 ② 已经连接的要有显示 ③ 按钮悬停/点击特效 ④ 识别中要有转圈齿轮」
//   「守护做成独立插件，与接口插件相连接来规定负责起对应的 agent，并且可选」
//   「1 默认全不选 + 全选按钮（勾选全选、不勾全不选）2 接不了的 agent 出另一套提示词 3 生成并接入弹窗里并入提示词」
//
// 两个后端插件：plugin.agentctl（scan/add/prompt/verify）· plugin.agent-guard（guard.set/status/restart）
// agents.prompt 现在两种：kind="attach"（能接的，给工具说明）与
// kind="onboard"（接不了的，出「接入手工单」：内核规矩 + 模板 + 落点 + 验收标准，接入它自己负责）。
// 本文件**自包含**（只用 document/fetch/navigator/window.confirm），便于用 Node+vm 打桩自测。
'use strict';

const AGENT_PLUGIN = 'plugin.agentctl';
const GUARD_PLUGIN = 'plugin.agent-guard';
const _AC_GEAR = '<span class="spin">⚙</span> ';
const _AC_TA_STYLE = 'width:100%;height:240px;background:var(--bg);color:var(--fg);border:1px solid var(--line);' +
                     'border-radius:6px;padding:8px;font-family:ui-monospace,Consolas,monospace;font-size:12px';
let _agents = [];
let _agentRisks = [];
let _agentModalKind = '';

// ── 工具 ─────────────────────────────────────────────────────
function _acEl(id) { return document.getElementById(id); }

async function _acCall(plugin, tool, args) {
  const r = await fetch('/api/bridge/plugins/call', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ plugin: plugin, tool: tool, args: args || {}, timeoutMs: 180000 }),
  });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const d = await r.json();
  if (d && d.data) return d.data;
  throw new Error((d && (d.error || 'plugin call failed')) || 'plugin call failed');
}

function _acNote(msg) { const n = _acEl('agentNote'); if (n) n.textContent = msg || ''; }

function _acState(msg, busy) {
  const n = _acEl('agentState');
  if (!n) return;
  if (busy) { n.innerHTML = _AC_GEAR + _acEscape(msg); } else { n.textContent = msg || ''; }
}

function _acBusy(id, on, label) {
  const b = _acEl(id);
  if (!b) return;
  if (!b.dataset) b.dataset = {};
  if (on) {
    if (b.dataset.orig === undefined) b.dataset.orig = b.textContent || '';
    b.disabled = true;
    b.innerHTML = _AC_GEAR + _acEscape(label || '');
  } else {
    b.disabled = false;
    if (b.dataset.orig !== undefined) { b.textContent = b.dataset.orig; }
  }
}

function _acEscape(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

const _AC_COLOR = { ok: 'var(--green)', warn: 'var(--amber)', bad: 'var(--red)', dim: 'var(--dim)' };
function _acTag(kind, text) {
  const c = _AC_COLOR[kind] || _AC_COLOR.dim;
  return `<span style="border:1px solid ${c};color:${c};border-radius:999px;padding:0 6px;font-size:11px">${_acEscape(text)}</span>`;
}

function _acConnected(a) { return !!(a && (a.connected || a.registered_only)); }
function _acPickable(a) { return !!(a && a.has_desc && !a.registered_only); }
function _acRiskOf(id) { return _agentRisks.filter((r) => r && r.id === id); }
function _acGuardOf(a) { return (a && a.guard && a.guard.available) ? a.guard : null; }

/** 勾选框们（排除禁用的） */
function _acBoxes() {
  const box = _acEl('agentList');
  if (!box || !box.querySelectorAll) return [];
  return Array.from(box.querySelectorAll('input.agent-pick')).filter((c) => !c.disabled);
}

/** 头上的「全选」跟着下面走：全勾=勾上、全不勾=不勾、勾一半=半选 */
function agentSyncAll() {
  const all = _acEl('agentAll');
  if (!all) return;
  const cs = _acBoxes();
  const on = cs.filter((c) => c.checked).length;
  all.checked = cs.length > 0 && on === cs.length;
  all.indeterminate = on > 0 && on < cs.length;
}

/** 点「全选」：勾上=全勾，去掉=全不勾 */
function agentToggleAll(on) {
  _acBoxes().forEach((c) => { c.checked = !!on; });
  agentSyncAll();
}

// ── ① 自动识别 ────────────────────────────────────────────────
async function agentScan() {
  _acState('识别中…', true); _acNote('正在扫本机 agent（描述文件 + 候选表 + 疑似 MCP 配置）…');
  _acBusy('agentScanBtn', true, '识别中…');
  try {
    const res = await _acCall(AGENT_PLUGIN, 'agents.scan', {});
    _agents = res.agents || [];
    _agentRisks = res.console_risks || [];
    agentRender(_agents);
    const conn = _agents.filter(_acConnected).map((x) => x.id);
    const att = _agents.filter(_acPickable).map((x) => x.id);
    const gd = _agents.filter((x) => { const g = _acGuardOf(x); return g && g.enabled; }).map((x) => x.id);
    _acState(`🔗 已连接 ${conn.length}${conn.length ? '：' + conn.join('、') : ''}` +
             ` ｜ 🛡 守护 ${gd.length}${gd.length ? '：' + gd.join('、') : ''}` +
             ` ｜ 识别到 ${_agents.length} 条（能操作 ${att.length}）`);
    let note = '默认**全不选**；勾一个，或点「全选」→「生成并接入」。' +
               '接不了的（没描述文件）点「📝 接入手工单」。';
    if (_agentRisks.length) {
      note += ' ｜ ⚠️ 还在用会闪黑窗的 python.exe：' +
        _agentRisks.map((r) => r.id + '·' + r.where).join('、') + '（勾上重接一次就换成 pythonw）';
    }
    _acNote(note);
  } catch (e) {
    _acState('识别失败'); _acNote(String(e.message || e));
  } finally {
    _acBusy('agentScanBtn', false);
  }
}

function agentRender(list) {
  const box = _acEl('agentList');
  if (!box) return;
  if (!list.length) { box.style.display = 'block'; box.innerHTML = '<div class="empty" style="padding:14px">没认到任何 agent</div>'; return; }
  const rows = list.map((a) => {
    const st = a.status || {};
    const can = !!a.has_desc;
    const connected = _acConnected(a);
    const g = _acGuardOf(a);
    const bits = [];
    if (connected) bits.push(_acTag('ok', '🔗 已连接'));
    if (a.registered_only) bits.push(_acTag('warn', '已登记·缺描述文件'));
    // config.yaml 里有这条、内核组件表里没有 = 内核还没读到（清单是启动快照）。
    // 以前这种只能靠"重启内核"消掉，现在点接入会自动热加载；这条标记是给**命令行加完条目**的情况看的。
    if (a.pending_restart) bits.push(_acTag('warn', '⏳ 待热加载（内核还没读到这条）'));
    if (a.has_desc) {
      if (!connected) {
        bits.push(_acTag(st.running ? 'warn' : 'dim',
          st.running ? '接口在跑，agent 侧还没接上' : (st.registered ? '已登记未跑' : '未接入')));
      }
      if (st.level) bits.push(_acTag('dim', '档位 ' + st.level));
      if (st.mcp_kind && st.mcp_kind !== 'none') {
        const hs = st.mcp_hosts || [];
        if (hs.length) {                       // 按面显示：MCP 1/2 面（claude-desktop 没装）
          const okN = hs.filter((x) => x.ok).length;
          const skip = hs.filter((x) => !x.exists).map((x) => x.name);
          const title = hs.map((x) => x.name + '：' + (x.ok ? '在位' : (x.exists ? '没配' : '没装'))).join('；');
          const col = (okN === hs.length) ? _AC_COLOR.ok : (okN ? _AC_COLOR.warn : _AC_COLOR.dim);
          bits.push(`<span title="${_acEscape(title)}" style="border:1px solid ${col};color:${col};border-radius:999px;padding:0 6px;font-size:11px">MCP ${okN}/${hs.length} 面${skip.length ? '（' + _acEscape(skip.join('、')) + ' 没装）' : ''}</span>`);
        } else {
          bits.push(_acTag(st.mcp_ok ? 'ok' : 'dim', 'MCP ' + (st.mcp_ok ? '在位' : '没配')));
        }
      }
      if (a.hook_kind && a.hook_kind !== 'none') bits.push(_acTag('dim', 'hook ' + a.hook_kind));
    } else if (!a.registered_only) {
      bits.push(_acTag('warn', a.unknown ? '疑似（没进表）' : '认到但没描述文件'));
    }
    if (g) {
      bits.push(_acTag(g.enabled ? 'ok' : 'dim', '🛡 守护 ' + (g.enabled ? '开' : '关')));
      if (g.enabled && g.alive === false) bits.push(_acTag('bad', '没在跑，正在拉'));
      if (g.starts) bits.push(_acTag('dim', '拉起 ' + g.starts + ' 次'));
    }
    if (_acRiskOf(a.id).length) bits.push(_acTag('bad', '⚠️ 命令会闪黑窗'));
    const ev = (a.evidence || []).map(_acEscape).join(' · ');
    const pickable = _acPickable(a);
    const gbtns = g ? `<button class="mini" onclick="agentGuard('${_acEscape(a.id)}', ${g.enabled ? 'false' : 'true'})">🛡 ${g.enabled ? '关守护' : '开守护'}</button>` +
                      (g.enabled ? `<button class="mini" onclick="agentRestart('${_acEscape(a.id)}')">↻ 重启</button>` : '') : '';
    const pbtn = can
      ? `<button class="mini" onclick="agentPrompt('${_acEscape(a.id)}')">提示词</button>` +
        `<button class="mini" onclick="agentVerify('${_acEscape(a.id)}')">验收</button>`
      : `<button class="mini" onclick="agentPrompt('${_acEscape(a.id)}')" title="这套还没找到直接接入的办法 —— 生成一份「接入手工单」（内核规矩 + 模板 + 落点 + 验收标准），让它自己接">📝 接入手工单</button>`;
    return `<div class="agent-row" style="display:flex;align-items:center;gap:10px;padding:6px 14px;border-bottom:1px solid var(--line2)">
      <input type="checkbox" class="agent-pick" value="${_acEscape(a.id)}"${pickable ? '' : ' disabled'} onclick="agentSyncAll()">
      <b style="min-width:150px">${_acEscape(a.name || a.id)}</b>
      <span style="color:var(--dim);font-size:12px">${_acEscape(a.id)}</span>
      ${bits.join(' ')}
      <span style="flex:1"></span>
      ${gbtns}
      ${pbtn}
    </div>`;
  }).join('');
  box.style.display = 'block';
  box.innerHTML = rows;
  agentSyncAll();                       // 默认全不选 → 头上的「全选」也是不勾
}

// ── ② 勾选 → 生成并接入（弹窗里连提示词一起给）────────────────
function agentPicked() {
  return _acBoxes().filter((c) => c.checked).map((c) => c.value);
}

// 内核侧接口：**不用重启内核** —— 调 POST /api/plugins/reload（只起新条目，不动在跑的）
async function agentHotReload() {
  try {
    const rr = await fetch('/api/plugins/reload', { method: 'POST', cache: 'no-store' });
    const rd = await rr.json();
    const r2 = (rd && rd.result) || {};
    const started = r2.started || [];
    const errs = r2.errors || {};
    let s = started.length
      ? '内核侧已热加载：' + started.map(_acEscape).join('、') + '（**不用**重启内核）'
      : '内核侧无需热加载（条目都已在跑）';
    if (Object.keys(errs).length) s += ' ｜ ⚠️ 起不来：' + _acEscape(JSON.stringify(errs));
    return s;
  } catch (e) {
    return '内核侧热加载没成（' + _acEscape(String(e.message || e)) + '）—— 退路：点上面「⟳ 重启内核」';
  }
}

async function agentAdd() {
  const ids = agentPicked();
  if (!ids.length) { _acNote('先勾一个（或点头上「全选」）'); return; }
  _acState('接入中…', true); _acNote('写 config.yaml / 档位 / agent 侧 MCP·hook…');
  _acBusy('agentAddBtn', true, '接入中…');
  try {
    const res = await _acCall(AGENT_PLUGIN, 'agents.add', { ids: ids, apply: true });
    const rows = res.results || [];
    const okRows = rows.filter((x) => x.ok);
    // 写完文件就让内核重读清单（2026-10-06 起不用再人肉点「重启内核」）
    const hot = okRows.length ? await agentHotReload() : '（没有新条目要热加载）';
    // 辞玖：「在生成并接入弹窗里合并上提示词窗口」→ 成功的一个一个把提示词也拉过来
    const prompts = [];
    for (const r of okRows) {
      try {
        const p = await _acCall(AGENT_PLUGIN, 'agents.prompt', { id: r.id });
        prompts.push({ id: r.id, name: p.name || r.id, text: p.prompt || '' });
      } catch (e) {
        prompts.push({ id: r.id, name: r.id, text: '（提示词没取到：' + String(e.message || e) + '）' });
      }
    }
    _acState(`已接入 ${okRows.length}/${ids.length}`);
    const detail = rows.map((r) =>
      `<div style="margin:4px 0"><b>${_acEscape(r.id)}</b> ` +
      (r.ok ? (r.steps || []).map((s) => `${_acEscape(s.step)}：${_acEscape(s.did)}`).join(' ｜ ')
            : '❌ ' + _acEscape(r.error || '')) + '</div>').join('');
    const blocks = prompts.map((p) =>
      `<div style="margin-top:12px">
         <div style="color:var(--fg2);font-size:12px;margin-bottom:4px">给「${_acEscape(p.name)}」的提示词 —— 复制给它（或它的主人）：</div>
         <textarea id="agentPromptBox_${_acEscape(p.id)}" readonly style="${_AC_TA_STYLE}">${_acEscape(p.text)}</textarea>
         <div style="margin-top:6px"><button class="mini" onclick="agentCopyPrompt('agentPromptBox_${_acEscape(p.id)}','agentCopyMsg_${_acEscape(p.id)}')">📋 复制这段</button>
         <span id="agentCopyMsg_${_acEscape(p.id)}" style="color:var(--green);font-size:12px;margin-left:8px"></span></div>
       </div>`).join('');
    _acNote(hot);
    agentModalOpen('接入结果 + 提示词',
      `<div style="font-size:12px">${detail}</div>
       <div style="margin-top:10px;color:var(--dim);font-size:12px">${hot}<br>剩下一步：**重启对应 agent**（宿主要重启才会加载新的 MCP 条目）→ 回来点「验收」。提示词现在就能复制给对方。</div>
       ${blocks}`);
    await agentScan();
  } catch (e) {
    _acState('接入失败'); _acNote(String(e.message || e));
  } finally {
    _acBusy('agentAddBtn', false);
  }
}

// ── ③ 守护开关 / 重启 ────────────────────────────────────────
async function agentGuard(id, on) {
  _acState((on ? '开' : '关') + '守护…', true);
  let msg = '';
  try {
    const res = await _acCall(GUARD_PLUGIN, 'guard.set', { id: id, enabled: on });
    if (res && res.ok === false) { throw new Error(res.error || '设置失败'); }
    msg = `${id} 守护已${on ? '打开' : '关闭'}（独立插件 plugin.agent-guard；它没在跑就把它静默拉起来）`;
  } catch (e) {
    msg = `${id} 守护设置失败：${String(e.message || e)}` +
          '（守护插件没在跑？控制台插件列表里启用 plugin.agent-guard）';
  } finally {
    await agentScan();
    _acNote(msg);
  }
}

async function agentRestart(id) {
  const warn = `重启「${id}」＝先把它杀掉，再由守护拉起来。\n` +
               (id === 'dsh' ? '⚠️ DSH 就是你现在这个会话，会被掐断（它几秒后自己回来）。\n' : '') +
               '继续？';
  if (typeof window !== 'undefined' && window.confirm && !window.confirm(warn)) { return; }
  _acState('重启中…', true);
  let msg = '';
  try {
    const res = await _acCall(GUARD_PLUGIN, 'guard.restart', { id: id });
    const killed = (res && res.killed) || [];
    msg = `${id}：杀掉 ${killed.length} 个进程（${killed.join(',') || '没匹配到'}），等它自己回来…`;
    _acNote(msg);
    for (let i = 0; i < 12; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      let st = null;
      try { st = await _acCall(GUARD_PLUGIN, 'guard.status', {}); } catch (e) { st = null; }
      const row = ((st && st.rows) || []).filter((x) => x && x.id === id)[0];
      if (row && row.alive) { msg = `${id} 回来了（守护拉起，共 ${row.starts || '?'} 次）`; _acNote(msg); break; }
      if (i === 11) { msg = `${id} 30 秒还没见它回来 —— 看 data/logs/agent-${id}.guard.err.log`; _acNote(msg); }
    }
  } catch (e) {
    msg = `${id} 重启失败：${String(e.message || e)}`;
  } finally {
    await agentScan();
    _acNote(msg);
  }
}

// ── ④ 提示词弹窗（能接的=提示词；接不了的=接入确认单）─────────
async function agentPrompt(id) {
  try {
    const res = await _acCall(AGENT_PLUGIN, 'agents.prompt', { id: id });
    const onboard = (res.kind === 'onboard');
    const head = onboard ? `「${res.name || id}」的接入手工单（复制给它，让它自己接）`
                         : `给「${res.name || id}」的提示词（可复制）`;
    const sub = onboard
      ? '内核这边的规矩、模板、每处的落点、验收标准都在里面 —— 接入由它全权负责；它接完回来我们点「验收」就行。'
      : `把它复制给那个 agent —— 工具怎么用、剩下那半截怎么接，都写在里面了。档位 ${_acEscape(res.level || '?')} · 放开 ${res.ops_count || 0} 条操作。`;
    agentModalOpen(head,
      `<div style="color:var(--dim);font-size:12px;margin-bottom:6px">${sub}</div>
       <textarea id="agentPromptBox" readonly style="${_AC_TA_STYLE};height:320px">${_acEscape(res.prompt || '')}</textarea>
       <div style="margin-top:8px"><button class="mini" onclick="agentCopyPrompt('agentPromptBox','agentCopyMsg')">📋 复制</button>
       <span id="agentCopyMsg" style="color:var(--green);font-size:12px;margin-left:8px"></span></div>`);
  } catch (e) {
    agentModalOpen('提示词生成失败', `<div style="color:var(--red)">${_acEscape(String(e.message || e))}</div>`);
  }
}

async function agentVerify(id) {
  try {
    const res = await _acCall(AGENT_PLUGIN, 'agents.verify', { id: id });
    const rows = (res.checks || []).map((c) =>
      `<div>${c.ok ? '✅' : '❌'} ${_acEscape(c.name)}${c.value ? '（' + _acEscape(c.value) + '）' : ''}</div>`).join('');
    agentModalOpen(`验收「${id}」：${res.ok ? '全部通过' : '有不过的'}`,
      `<div style="font-size:13px;line-height:1.9">${rows}</div>
       <div style="margin-top:10px;color:var(--dim);font-size:12px">MCP 那一栏没绿 = 那个 agent 侧还没接上（看上面「提示词」里怎么接）；
       「不弹黑窗」那一栏没绿 = 它那条命令还在用 python.exe，勾上它点「生成并接入」重接一次即可。</div>`);
  } catch (e) {
    agentModalOpen('验收失败', `<div style="color:var(--red)">${_acEscape(String(e.message || e))}</div>`);
  }
}

// ── ⑤ 弹窗 / 复制 ────────────────────────────────────────────
function agentModalOpen(title, bodyHtml) {
  let m = _acEl('agentModal');
  if (!m) {
    m = document.createElement('div');
    m.id = 'agentModal';
    m.className = 'modal-mask';
    document.body.appendChild(m);
  }
  _agentModalKind = title;
  m.className = 'modal-mask show';
  m.innerHTML = `<div class="modal" style="width:760px">
      <button class="close" onclick="agentCloseModal()">✕</button>
      <h3>${_acEscape(title)}</h3>
      <div id="agentModalBody">${bodyHtml}</div>
    </div>`;
}

function agentCloseModal() {
  const m = _acEl('agentModal');
  if (m) { m.className = 'modal-mask'; m.innerHTML = ''; }
}

async function agentCopyPrompt(boxId, msgId) {
  const box = _acEl(boxId || 'agentPromptBox');
  if (!box) return;
  const text = box.value || box.textContent || '';
  let ok = false;
  try {
    if (navigator && navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text); ok = true;
    }
  } catch (e) { ok = false; }
  if (!ok && box.select) { try { box.select(); document.execCommand('copy'); ok = true; } catch (e) { ok = false; } }
  const msg = _acEl(msgId || 'agentCopyMsg');
  if (msg) msg.textContent = ok ? '已复制' : '复制失败 —— 手动全选复制吧';
}

// 面板一出现就识别一次：**已经连接的进来就能看见**，不用先点按钮
if (typeof document !== 'undefined' && document.addEventListener) {
  document.addEventListener('DOMContentLoaded', function () {
    _acState('未识别');
    if (_acEl('agentList')) { agentScan(); }
  });
}