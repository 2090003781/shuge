// 枢阁前端：纯 API 客户端（只调 /api，不碰本地）
// 双视图：笔记(#/notes) / 控制台(#/console)，hash 路由切换
// 笔记视图 = 通用文件树（任意格式 + 多层文件夹）

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

// ---- 视图切换 ----
// 视图映射：**只列 index.html 里真实存在的视图**。
// 2026-09-20 踩过的坑：映射里留着一个已经删掉的视图（库 iframe）→ getElementById 返回 null →
// 初始化那次 go(...) 就抛错，后面的 tickConsole() 没跑，于是"插件列表根本没加载出来"。
// 规矩：改 index.html 的视图，就同步改这里；go() 也对缺失元素容错，不再一错连坐整页。
const VIEWS = { vault: 'view-vault', console: 'view-console', trash: 'view-trash', detail: 'view-detail' };
const NAVS = { console: 'nav-console', trash: 'nav-trash' };

function go(name) {
  // 详情路由：#/detail/<kind>/<id>
  if (name.startsWith('detail/')) {
    const parts = name.split('/'); // detail / kind / id
    showDetailPage(decodeURIComponent(parts[1] || ''), decodeURIComponent(parts.slice(2).join('/') || ''));
    return;
  }
  if (!VIEWS[name]) name = 'console';
  for (const k in VIEWS) {
    const el = document.getElementById(VIEWS[k]);
    if (el) el.classList.toggle('active', k === name);
  }
  for (const k in NAVS) {
    const el = document.getElementById(NAVS[k]);
    if (el) el.classList.toggle('active', k === name);
  }
  location.hash = name;
  if (name === 'vault') loadTree();          // 旧 Vault 视图（备用）
  if (name === 'console') tickConsole();
  if (name === 'trash') loadTrash();
}
window.go = go;

// 跳转组件详情页
function goDetail(kind, id) {
  location.hash = 'detail/' + encodeURIComponent(kind) + '/' + encodeURIComponent(id);
  showDetailPage(kind, id);
}
window.goDetail = goDetail;

// 组件详情页渲染
async function showDetailPage(kind, id) {
  // 复位实时账本开关（renderDetail 渲染账本组件时再开启）
  window.__ledgerOpen = false;
  // 切到 detail 视图（VIEWS 含 detail；NAVS 不含 → 顶栏不高亮任何项）
  for (const k in VIEWS) {
    document.getElementById(VIEWS[k]).classList.toggle('active', k === 'detail');
    if (NAVS[k]) document.getElementById(NAVS[k]).classList.toggle('active', false);
  }
  const body = document.getElementById('detailBody');
  body.innerHTML = '<div style="color:var(--dim)">加载详情...</div>';
  try {
    const d = await fetchJSON('/api/component-detail?kind=' + encodeURIComponent(kind) + '&id=' + encodeURIComponent(id));
    renderDetail(body, d);
  } catch (e) {
    body.innerHTML = '<div style="color:var(--red)">加载失败: ' + e + '</div>';
  }
}

// ── 软件操作权限：读 / 存（走平台接口 /api/permissions）────────────────
let __permState = null;

async function loadPermissions(pluginId) {
  const box = document.getElementById('permLevels');
  if (!box) return;
  try {
    const p = await fetchJSON('/api/permissions?plugin=' + encodeURIComponent(pluginId));
    __permState = p;
    box.innerHTML = (p.levels || []).map((lv) => `
      <label style="display:flex;align-items:flex-start;gap:8px;cursor:pointer">
        <input type="radio" name="permLevel" value="${lv.id}" ${p.level === lv.id ? 'checked' : ''}
               onchange="savePermissionsNow('${esc(pluginId)}')">
        <span><b style="color:var(--blue)">${esc(lv.label)}</b>
          <span style="color:var(--dim)">${esc(lv.desc || '')}</span></span>
      </label>`).join('');
    const cur = (p.levels || []).find((x) => x.id === p.level);
    const msg = document.getElementById('permMsg');
    if (msg) msg.textContent = `当前「${cur ? cur.label : p.level}」：释放 ${p.released}/${p.total} 个操作`;
    renderPermOps(p);
  } catch (e) {
    box.innerHTML = `<span style="color:var(--red)">读权限失败：${esc(String(e))}</span>`;
  }
}

// 清单渲染（读的时候用，点即存之后也用返回值刷新，不重建整块 DOM）
function renderPermOps(p) {
  const ops = document.getElementById('permOps');
  if (!ops) return;
  const cnt = document.getElementById('permOpsCount');
  if (cnt) cnt.textContent = (p.catalog || []).length;   // 条数按目录实际算，别写死
  const byLevel = { read: [], write: [], trust: [] };
  (p.catalog || []).forEach((o) => { (byLevel[o.level] || (byLevel[o.level] = [])).push(o); });
  ops.innerHTML = ['read', 'write', 'trust'].map((lv) => {
    const label = { read: '只读', write: '可写', trust: '相信' }[lv];
    const list = (byLevel[lv] || []).map((o) =>
      `<div style="display:flex;gap:8px;padding:2px 0"><span>${o.released ? '✅' : '⛔'}</span>` +
      `<span style="min-width:150px;color:var(--blue)">${esc(o.id)}</span>` +
      `<span style="color:var(--dim)">${esc(o.op)}</span></div>`).join('');
    return `<div style="margin-top:6px"><b>${label}（${(byLevel[lv] || []).length}）</b>${list}</div>`;
  }).join('');
}

// 点即存（视觉 = 实际）：保存期间把选项禁用，别人为点得比一次往返还快；
// 回来之后按**服务器确认的档位**回填勾选 —— 屏幕上看到的，就是已经落库的那一档。
function setPermInputsDisabled(on) {
  document.querySelectorAll('input[name="permLevel"]').forEach((el) => { el.disabled = on; });
}

async function savePermissionsNow(pluginId) {
  const sel = document.querySelector('input[name="permLevel"]:checked');
  if (!sel) return;
  const msg = document.getElementById('permMsg');
  setPermInputsDisabled(true);
  if (msg) msg.textContent = '保存中…';
  try {
    const r = await fetchJSON('/api/permissions/set', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plugin: pluginId, level: sel.value }),
    });
    document.querySelectorAll('input[name="permLevel"]').forEach((el) => {
      el.checked = el.value === r.level;          // 回填成服务器确认的档位
    });
    const cur = (r.levels || []).find((x) => x.id === r.level);
    if (msg) msg.textContent = `已保存：「${cur ? cur.label : r.level}」，释放 ${r.released}/${r.total} 个操作`;
    renderPermOps(r);
  } catch (e) {
    if (msg) msg.textContent = '保存失败：' + e;
  } finally {
    setPermInputsDisabled(false);
  }
}

async function savePermissions(pluginId) {
  const sel = document.querySelector('input[name="permLevel"]:checked');
  const msg = document.getElementById('permMsg');
  if (!sel) return;
  try {
    const r = await fetchJSON('/api/permissions/set', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plugin: pluginId, level: sel.value }),
    });
    const cur = (r.levels || []).find((x) => x.id === r.level);
    if (msg) msg.textContent = `已保存：「${cur ? cur.label : r.level}」，释放 ${r.released}/${r.total} 个操作`;
  } catch (e) {
    if (msg) msg.textContent = '保存失败：' + e;
  }
}

function renderDetail(body, d) {
  const kindLabel = { plugin: '插件', module: '模块', agent: 'Agent', system: '系统' };
  const kindBadge = { plugin: 'tool', module: 'auto', agent: 'guard', system: 'warn' };
  const si = statusInfo(d.status, d.kind);
  let html = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;flex-wrap:wrap">
      <button class="mini" onclick="go('console')">← 返回控制台</button>
      <span class="badge ${kindBadge[d.kind] || 'tool'}">${kindLabel[d.kind] || d.kind}</span>
      <h2 style="font-size:18px;color:var(--blue);margin:0">${esc(d.name || d.id)}</h2>
      <span class="badge ${si.cls}">${si.text}</span>
    </div>
    <div style="color:var(--dim);font-size:13px;margin-bottom:12px">${esc(d.id)}</div>`;

  if (d.desc) html += `<div style="background:var(--panel3);border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:14px;line-height:1.7"><b style="color:var(--fg2)">作用</b><br>${esc(d.desc)}</div>`;

  // 身份声明：就是本插件的一个字段（2026-09-12 辞玖定"身份申明加通道放一起"）
  if (d.agent_id) {
    html += `<div style="background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:10px;line-height:1.7">
      🧠 <b style="color:var(--fg2)">代理的身份</b>：<span style="color:var(--blue)">${esc(d.agent_id)}</span>
      ${d.agent_name ? `<span style="color:var(--dim)">（${esc(d.agent_name)}）</span>` : ''}
      <div style="color:var(--fg2);margin-top:4px">${esc(d.agent_description || '')}</div>
      <div style="color:var(--dim);margin-top:4px">平台按这个身份路由定向事件（Inject 目标可用 ${esc(d.agent_id)} 或 ${esc(d.id)}）、把会话与事件记在它名下。</div>
    </div>`;
  }


  // 软件操作权限（三档：只读 / 可写 / 相信）—— 所有操作封装在平台侧（辞玖 2026-09-19 定）。
  // 只有 agent 接口插件（注册时自报 agent_id）才显示这块。
  if (d.agent_id) {
    html += `<div style="background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:10px;line-height:1.7">
      🔐 <b style="color:var(--fg2)">软件操作权限</b>
      <span style="color:var(--dim)">放到哪一档，就把那一档（含更低档）的软件操作交给这个 agent；库页面上的按钮不受影响。<b>点一下就存</b>；保存期间选项会短暂变灰——看到的勾选就是已经落库的结果。</span>
      <div id="permLevels" style="margin-top:8px;display:flex;flex-direction:column;gap:6px"><span style="color:var(--dim)">加载权限…</span></div>
      <details style="margin-top:8px"><summary style="cursor:pointer;color:var(--blue)">这档具体放开哪些操作（<span id="permOpsCount">…</span> 条）</summary>
        <div id="permOps" style="margin-top:6px;color:var(--fg2)"></div>
      </details>
      <div style="margin-top:10px;display:flex;gap:8px;align-items:center">
        <span id="permMsg" style="color:var(--dim)"></span>
      </div>
    </div>`;
  }

  // 通用信息行
  const rows = [['ID', d.id], ['类型', d.type], ['状态', d.status], ['能力', (d.capabilities || []).join(', ') || '-']];
  html += `<table><tbody>${rows.map(([k, v]) => `<tr><td style="width:110px;color:var(--dim)">${k}</td><td>${esc(String(v))}</td></tr>`).join('')}</tbody></table>`;

  // meta 键值
  if (d.meta && Object.keys(d.meta).length) {
    html += `<h2 class="sec">详情信息</h2><table><tbody>`;
    for (const [k, v] of Object.entries(d.meta)) {
      const val = Array.isArray(v) ? v.map(esc).join('<br>') : esc(String(v));
      html += `<tr><td style="width:110px;color:var(--dim)">${esc(k)}</td><td style="word-break:break-all">${val}</td></tr>`;
    }
    html += `</tbody></table>`;
  }

  // 插件进程信息
  if (d.process) {
    html += `<h2 class="sec">进程状态</h2><table><tbody>`;
    html += `<tr><td style="width:110px;color:var(--dim)">运行中</td><td>${d.process.running ? '是' : '否'}</td></tr>`;
    if (d.process.restarts !== undefined) html += `<tr><td style="color:var(--dim)">重启次数</td><td>${d.process.restarts}</td></tr>`;
    if (d.process.stopped !== undefined) html += `<tr><td style="color:var(--dim)">主动停止</td><td>${d.process.stopped ? '是' : '否'}</td></tr>`;
    html += `</tbody></table>`;
  }

  // 插件操作区（启用/禁用/删除 → 会话级，操作后刷新详情页）
  if (d.kind === 'plugin') {
    const st = d.status || 'running';
    const isOn = st === 'running';
    html += `<h2 class="sec">操作</h2>
    <div style="background:${isOn ? 'var(--ok-bg)' : 'var(--warn-bg)'};border:1px solid ${isOn ? 'var(--green2)' : 'var(--brown2)'};color:${isOn ? 'var(--green)' : 'var(--amber)'};border-radius:8px;padding:10px 14px;font-size:14px;margin-bottom:10px">
      ${isOn ? '🟢 当前状态：已启用（插件正在运行，事件/功能可用）' : '🟠 当前状态：已禁用（插件已停止，重启后按配置恢复）'}
    </div>
    <div style="display:flex;gap:8px;margin-bottom:4px">
      ${!isOn ? `<button class="act" onclick="detailPluginAction('start','${d.id}')">▶ 启用</button>` : `<button class="act" style="background:var(--dim)" onclick="detailPluginAction('stop','${d.id}')">⏸ 禁用</button>`}
      <button class="act" style="background:var(--red)" onclick="detailPluginAction('delete','${d.id}')">🗑 移入回收站</button>
      <span id="dpMsg" style="align-self:center;font-size:12px;color:var(--green)"></span>
    </div>
    <div style="color:var(--dim);font-size:12px;margin-bottom:8px">移入回收站可恢复；重启后不自动启动（去控制台「插件回收站」恢复）</div>`;
  }

  // 事件类组件定制：日志模块 / 事件总线 → 查询面板 + 实时账本 + 拓扑
  if ((d.kind === 'system' && d.id === 'event-bus') || (d.kind === 'module' && d.id === 'module.event-log')) {
    html += renderEventQueryPanel();
    window.__ledgerOpen = true;
    html = renderLedgerPanel(html);
    if (d.kind === 'system' && d.id === 'event-bus') {
      html += renderTopologyPanel();
    }
    body.innerHTML = html;
    setTimeout(reloadLedger, 50);
    if (d.kind === 'system' && d.id === 'event-bus') {
      setTimeout(loadTopology, 80);
    }
    return;
  }
  // 上下文包插件：注入路由开关面板（agent 级启用/禁用）
  if (d.kind === 'plugin' && d.id === 'plugin.context-pack') {
    html += renderContextRoutesPanel();
  }

  body.innerHTML = html;
  // 权限块要等 DOM 落地后再拉（拼串阶段 fetch 时 #permLevels 还不存在 → 界面会卡在"加载权限…"）
  if (d.agent_id) setTimeout(() => loadPermissions(d.id), 0);
  if (d.kind === 'plugin' && d.id === 'plugin.context-pack') {
    setTimeout(() => { loadCtxPanel(); }, 60);
  }
}

// ---- 上下文包插件调用面（2026-09-13 前移）----
// 注意：这些必须在下面的面板函数**之前**定义——`loadCtxPanel()` 在页面加载时就被调用，
// 而 `const` 不提升；原先定义在文件后段 → 页面报
// "Cannot access 'CTXPACK_PLUGIN' before initialization"（实测踩到）。
const CTXPACK_PLUGIN = 'plugin.context-pack';
const PACK_MAX_FILES = 20;

// ── 展开/收起 与 文件夹级注入开关（2026-09-13 稳定区）──
// 【为什么放在这儿】这两段曾两次因为「大块切片替换」被连带删除（改渲染区/选择器区时）。
// 现固定在调用面常量之后、面板函数之前——此处不再被后续编辑的切片区间覆盖。
// 展开 key：'folder:<agent>' | 'pack:<id>'
function toggleCtxExpand(key) {
  window.__ctxExpanded[key] = !window.__ctxExpanded[key];
  renderCtxBoard();
}

// 文件夹级注入开关：docs:<agent> → POST /api/context/route-set（agent 级 enable）
async function toggleCtxInject(key, enabled) {
  try {
    if (key.indexOf('docs:') === 0) {
      const agent = key.slice(5);
      await fetchJSON('/api/context/route-set', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent, enabled: !!enabled })
      });
    } else {
      await callCtxTool('pack.update', { id: key.slice(5), enabled: !!enabled });
    }
    await loadCtxPanel();
  } catch (e) {
    alert('切换注入失败: ' + (e.message || e));
    await loadCtxPanel();
  }
}

async function callCtxTool(tool, args) {
  const r = await fetchJSON('/api/bridge/plugins/call', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ plugin: CTXPACK_PLUGIN, tool, args: args || {}, timeoutMs: 40000 }),
  });
  if (!r.ok) throw new Error(r.error || '插件没回执（看它的 poll 循环是否活着）');
  return r.data;
}

// ---- 注入包面板（2026-09-13 统一卡片视图）----
// 结构：① 各 agent 注入开关总表 → ② 统一「注入包」卡片列表（路由文档 + 命名包同构）。
// 一张卡 = 一个包；卡头点开 = 文件列表；每行文件可单文件启停/移除；卡内可加文件/删除/恢复默认。
function renderContextRoutesPanel() {
  return `
    <h2 class="sec">📦 注入包 <button class="mini" onclick="loadCtxPanel()">↻ 刷新</button>
      <button class="mini" onclick="newNamedPack()">＋ 新建包</button></h2>
    <div style="color:var(--dim);font-size:12px;margin-bottom:8px">
      每张卡 = 一个注入包，卡头右侧有它自己的<b>注入开关</b>（路由文档卡 = 该 agent 是否注入；命名包卡 = 该包是否参与组包）。
      点卡头展开文件列表：每行一个文件，勾选框 = 启用/停用（停用 = 条目留着、内容不进包），✕ = 移除，
      「＋ 从库中选择文件」从库里挑。两类同构：<b>📄 路由文档</b>（每 agent 一份，默认来自平台 config，
      可含 persona/profile/rules-l1 关键字）与 <b>📦 命名包</b>（自己挑笔记，可指定目标 agent）。
      文件在磁盘上一改，插件自动重新组包（事件触发 + 定时兜底 + 包变更）。
    </div>
    <div id="packFormBox"></div>
    <div id="ctxBoardBox"><div style="color:var(--dim)">加载中...</div></div>`;
}

// 清单条目可能是字符串（老格式）或 {path, enabled} —— 统一成 {path, enabled}
function fileEntries(list) {
  return (list || []).map(x => (typeof x === 'string')
    ? { path: x, enabled: true }
    : { path: x.path, enabled: x.enabled !== false });
}
function enabledCount(list) { return fileEntries(list).filter(e => e.enabled).length; }

// 一次拉全：路由开关 + 统一包列表（都来自插件 pack.list，卡片渲染不依赖 /api/context/routes）
async function loadCtxPanel() {
  let d;
  try {
    d = await callCtxTool('pack.list', {});
  } catch (e) {
    const b = document.getElementById('ctxBoardBox');
    if (b) b.innerHTML = '<div style="color:var(--red)">取插件清单失败: ' + esc(e.message || e) + '</div>';
    return;
  }
  window.__ctxMeta = d;
  window.__ctxAgents = d.agents || [];
  // 注入开关合并进卡片后，仍需要路由的 enabled/包状态（agent 级开关 + 包大小/更新时间）
  try { window.__ctxRoutes = await fetchJSON('/api/context/routes'); }
  catch (e) { window.__ctxRoutes = window.__ctxRoutes || []; }
  renderCtxBoard();
}

// ── 三级视图（2026-09-13 四次改造）：agent 文件夹 → 包 → 文件 ──
// 展开状态：{ 'folder:<agent>': bool, 'pack:<id>': bool }
if (!window.__ctxExpanded) window.__ctxExpanded = {};

function renderCtxBoard() {
  const box = document.getElementById('ctxBoardBox');
  if (!box) return;
  const agents = window.__ctxAgents || [];
  const packs = (window.__ctxMeta && window.__ctxMeta.packs) || [];
  const routes = window.__ctxRoutes || [];
  const meta = window.__ctxMeta || {};
  if (!agents.length && !packs.length) {
    box.innerHTML = '<div style="color:var(--dim)">还没有任何注入文件夹</div>';
    return;
  }
  const generic = packs.filter(p => !p.owner);
  const folders = agents.map(a => ({ agent: a.agent, route: routes.find(r => r.agent === a.agent) || {}, info: a,
                                     packs: packs.filter(p => p.owner === a.agent) }));
  box.innerHTML = folders.map(f => renderFolder(f)).join('')
    + (generic.length ? renderFolder({ agent: '', route: {}, info: null, packs: generic }) : '')
    + `<div style="color:var(--dim);font-size:11px;margin-top:6px">清单：${esc(meta.path || '-')}｜路由文档覆盖：${esc(meta.routes_docs_path || '-')}｜单文件最多 ${meta.file_chars || '-'} 字符（超出自动截断）</div>`;
}

function renderFolder(f) {
  const isGeneric = !f.agent;
  const key = 'folder:' + (f.agent || '__generic');
  const open = !!window.__ctxExpanded[key];
  const docs = fileEntries(f.info ? f.info.docs : []);
  const name = isGeneric ? '通用包（未指定所属 agent）' : f.agent;
  const route = f.route || {};
  const packInfo = route.packBytes ? ((route.packBytes / 1024).toFixed(1) + ' KB · ' + ((route.packUpdated || '').replace('T', ' ').slice(11, 19) || '-')) : (isGeneric ? '' : '未生成');
  return `<div style="border:1px solid var(--line);border-radius:10px;margin-bottom:12px;overflow:hidden">
    <div onclick="toggleCtxExpand('${key}')" style="display:flex;gap:10px;align-items:center;padding:10px 12px;cursor:pointer;background:var(--bg5)">
      <span style="color:var(--blue);font-size:14px">${open ? '▾' : '▸'} 📁 ${esc(name)}</span>
      ${isGeneric ? '<span class="badge">通用</span>' : `<span class="badge ${route.enabled ? 'ok' : 'warn'}">${route.enabled ? '✅ 注入中' : '⛔ 未注入'}</span>`}
      <span style="color:var(--dim);font-size:11px">${f.packs.length} 个包${docs.length ? ' · 默认文档 ' + enabledCount(f.info && f.info.docs) + '/' + docs.length : ''}${packInfo ? ' · ' + esc(packInfo) : ''}</span>
      <span style="flex:1"></span>
      ${isGeneric ? '' : `<button class="mini" style="background:${route.enabled ? 'var(--ok-bg)' : 'var(--line)'}"
        onclick="event.stopPropagation();toggleCtxInject('docs:${esc(f.agent)}', ${route.enabled ? 'false' : 'true'})">
        ${route.enabled ? '⏸ 关闭注入' : '▶ 开启注入'}</button>`}
    </div>
    ${open ? `<div style="padding:8px 12px 12px 12px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <span style="color:var(--dim);font-size:12px">📦 包（${f.packs.length + (isGeneric ? 0 : 1)}）</span>
        <button class="mini" onclick="newPackIn('${esc(f.agent)}')">＋ 新建包</button>
      </div>
      ${isGeneric ? '' : renderDefaultPackCard(f)}
      ${f.packs.map(p => renderPackCard(p)).join('')}
    </div>` : ''}
  </div>`;
}

// 默认包卡片：形态与命名包一致（名字/开关/文件列表/加文件/预览），额外带「↺ 恢复默认」
function renderDefaultPackCard(f) {
  const agent = f.agent;
  const key = 'docs:' + agent;
  const info = f.info || {};
  const open = !!window.__ctxExpanded['folder:' + agent];
  const entries = fileEntries(info.docs);
  const route = f.route || {};
  const title = info.name || '默认包';
  return `<div style="border:1px solid var(--line);border-radius:8px;margin-bottom:6px;overflow:hidden">
    <div onclick="toggleCtxExpand('folder:${esc(agent)}')" style="display:flex;gap:8px;align-items:center;padding:8px 10px;cursor:pointer;background:var(--panel)">
      <span style="color:var(--fg);font-size:13px">${open ? '▾' : '▸'} 📦 ${esc(title)}</span>
      <span class="badge">${info.docs_overridden ? '控制台改过' : '平台默认'}</span>
      <span style="color:var(--dim);font-size:11px">${enabledCount(info.docs)}/${entries.length} 文件启用</span>
      ${(info.docs_missing || []).length ? '<span style="color:var(--red);font-size:11px">有缺失文件</span>' : ''}
      <span style="flex:1"></span>
      <button class="mini" style="background:${route.enabled ? 'var(--ok-bg)' : 'var(--line)'}"
        onclick="event.stopPropagation();toggleCtxInject('docs:${esc(agent)}', ${route.enabled ? 'false' : 'true'})">
        ${route.enabled ? '⏸ 关闭注入' : '▶ 开启注入'}</button>
    </div>
    ${open ? `<div style="padding:4px 10px 10px 10px">
      ${entries.map((e, i) => fileRow(key, e, i)).join('') || '<div style="color:var(--amber);font-size:12px">（空包）</div>'}
      ${(info.docs_missing || []).length ? `<div style="color:var(--red);font-size:11px">找不到：${esc(info.docs_missing.join('、'))}</div>` : ''}
      <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">
        <button class="mini" onclick="addCtxFile('${key}')">＋ 从库中选择文件</button>
        <button class="mini" onclick="previewCtxCard('${key}')">👀 看看内容</button>
        <button class="mini" onclick="openPackSettings('${esc(agent)}')">✎ 设置</button>
        <button class="mini" onclick="resetCtxDocs('${esc(agent)}')">↺ 恢复默认</button>
      </div>
      <div id="ctxCardPreview-${key}" style="font-size:12px;max-height:200px;overflow-y:auto;margin-top:8px"></div>
    </div>` : ''}
  </div>`;
}

function fileRow(key, e, i) {
  return `<div style="display:flex;gap:8px;align-items:center;padding:2px 0 2px 8px">
      <label style="cursor:pointer" title="取消勾选=停用（条目留着，内容不进包）">
        <input type="checkbox" ${e.enabled ? 'checked' : ''} onchange="toggleCtxFile('${key}', ${i}, this.checked)"></label>
      <span style="color:${e.enabled ? 'var(--fg)' : 'var(--dim)'};font-size:12px;${e.enabled ? '' : 'text-decoration:line-through'}">${esc(e.path)}</span>
      <button class="mini" onclick="removeCtxEntry('${key}', ${i})">✕</button>
    </div>`;
}

function renderPackCard(p) {
  const key = 'pack:' + p.id;
  const open = !!window.__ctxExpanded[key];
  const entries = fileEntries(p.files);
  const agentsTxt = (p.agents || []).join('、') || '所有 agent';
  return `<div style="border:1px solid var(--line);border-radius:8px;margin-bottom:6px;overflow:hidden">
    <div onclick="toggleCtxExpand('${key}')" style="display:flex;gap:8px;align-items:center;padding:8px 10px;cursor:pointer;background:var(--panel)">
      <span style="color:var(--fg);font-size:13px">${open ? '▾' : '▸'} 📦 ${esc(p.name)}</span>
      ${p.enabled === false ? '<span class="badge warn">⛔ 已停用</span>' : ''}
      <span class="badge">${esc(agentsTxt)}</span>
      <span style="color:var(--dim);font-size:11px">${enabledCount(p.files)}/${entries.length} 文件启用</span>
      ${(p.files_missing || []).length ? '<span style="color:var(--red);font-size:11px">有缺失文件</span>' : ''}
      <span style="flex:1"></span>
      <button class="mini" style="background:${p.enabled === false ? 'var(--line)' : 'var(--ok-bg)'}"
        onclick="event.stopPropagation();toggleCtxPack('${esc(p.id)}', ${p.enabled === false ? 'true' : 'false'})">
        ${p.enabled === false ? '▶ 启用' : '⏸ 停用'}</button>
      <button class="mini" onclick="event.stopPropagation();deleteCtxPack('${esc(p.id)}')">🗑</button>
    </div>
    ${open ? `<div style="padding:4px 10px 10px 10px">
      ${entries.map((e, i) => fileRow(key, e, i)).join('') || '<div style="color:var(--amber);font-size:12px">（空包）</div>'}
      ${(p.files_missing || []).length ? `<div style="color:var(--red);font-size:11px">找不到：${esc(p.files_missing.join('、'))}</div>` : ''}
      <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">
        <button class="mini" onclick="addCtxFile('${key}')">＋ 从库中选择文件</button>
        <button class="mini" onclick="previewCtxCard('${key}')">👀 看看内容</button>
        <button class="mini" onclick="openPackSettings('${esc(p.id)}')">✎ 设置</button>
      </div>
      <div id="ctxCardPreview-${key}" style="font-size:12px;max-height:200px;overflow-y:auto;margin-top:8px"></div>
    </div>` : ''}
  </div>`;
}

// 包级开关 / 删除（带确认）
async function toggleCtxPack(id, enabled) {
  try {
    await callCtxTool('pack.update', { id, enabled: !!enabled });
    await loadCtxPanel();
    window.__ctxExpanded['pack:' + id] = true;
    renderCtxBoard();
  } catch (e) { alert('切换失败: ' + (e.message || e)); await loadCtxPanel(); }
}

async function deleteCtxPack(id) {
  const p = ((window.__ctxMeta && window.__ctxMeta.packs) || []).find(x => x.id === id);
  if (!confirm('删除包「' + ((p && p.name) || id) + '」？（只删清单条目，Vault 里的文件不动）')) return;
  try {
    await callCtxTool('pack.delete', { id });
    await loadCtxPanel();
  } catch (e) { alert('删除失败: ' + (e.message || e)); }
}

// 从展开卡片的 key 找回包对象
function findCtxCard(key) {
  if (key.indexOf('docs:') === 0) {
    const agent = key.slice(5);
    const a = (window.__ctxAgents || []).find(x => x.agent === agent);
    return a ? { kind: 'docs', agent, files: a.docs, restore: true, name: a.name || '默认包' } : null;
  }
  const id = key.slice(5);   // 'pack:<id>'
  const p = ((window.__ctxMeta && window.__ctxMeta.packs) || []).find(x => x.id === id);
  return p ? { kind: 'named', id, files: p.files, name: p.name, agents: p.agents || [] } : null;
}

// ---- 统一卡片操作：单文件启停 / 移除 / 加文件 / 预览 / 新建改名删除 ----

// 单文件启停（含即时落盘）
async function toggleCtxFile(key, idx, on) {
  const c = findCtxCard(key);
  if (!c) return;
  const entries = fileEntries(c.files);
  const e = entries[idx];
  if (!e) return;
  e.enabled = !!on;
  try {
    if (c.kind === 'docs') {
      await callCtxTool('pack.set-file', { kind: 'docs', agent: c.agent, path: e.path, enabled: !!on });
    } else {
      await callCtxTool('pack.update', { id: c.id, files: entries });
    }
    await loadCtxPanel();
    window.__ctxExpanded[key] = true;   // 操作后保持展开，免得每次都要重新点开
    renderCtxBoard();
  } catch (err) {
    alert('切换失败: ' + (err.message || err));
    await loadCtxPanel();
  }
}

// 移除清单里的某条（整条删；不动 Vault 文件）
async function removeCtxEntry(key, idx) {
  const c = findCtxCard(key);
  if (!c) return;
  const entries = fileEntries(c.files).filter((_, i) => i !== idx);
  try {
    if (c.kind === 'docs') {
      await callCtxTool('pack.entries', { agent: c.agent, files: entries });
    } else {
      await callCtxTool('pack.update', { id: c.id, files: entries });
    }
    await loadCtxPanel();
    window.__ctxExpanded[key] = true;
    renderCtxBoard();
  } catch (e) { alert('移除失败: ' + (e.message || e)); }
}

// 给某张卡片加文件（Vault 选择器，选中即落盘）
function addCtxFile(key) {
  const c = findCtxCard(key);
  if (!c) return;
  window.__packEdit = { id: c.kind === 'named' ? c.id : '', files: fileEntries(c.files) };
  window.__pickMode = (c.kind === 'docs') ? { kind: 'docs', agent: c.agent } : { kind: 'named', id: c.id };
  window.__ctxExpanded[(c.kind === 'docs') ? ('folder:' + c.agent) : ('pack:' + c.id)] = true;
  document.getElementById('vaultPickModal').classList.add('show');
  loadVaultFileList().then(() => renderVaultPick());
}

// 预览某张卡片的文件内容（不落盘）
async function previewCtxCard(key) {
  const box = document.getElementById('ctxCardPreview-' + key);
  if (!box) return;
  const c = findCtxCard(key);
  if (!c) return;
  const files = fileEntries(c.files).filter(e => e.enabled).map(e => e.path);
  if (!files.length) { box.innerHTML = '<span style="color:var(--amber)">没有启用的文件</span>'; return; }
  box.innerHTML = '<span style="color:var(--dim)">读取中…</span>';
  try {
    const d = await callCtxTool('pack.preview', { files });
    box.innerHTML = (d.files || []).map(f => `<div style="border-left:2px solid var(--line);padding-left:8px;margin-bottom:8px">
        <div style="color:var(--blue)">${esc(f.path)}
          <span style="color:var(--dim)">${f.chars} 字符${f.ok ? '' : '｜读取失败：' + esc(f.err || '')}</span></div>
        <div style="color:var(--dim);white-space:pre-wrap">${esc((f.head || '').slice(0, 200))}</div>
      </div>`).join('') || '<span style="color:var(--dim)">没有可预览的内容</span>';
  } catch (e) {
    box.innerHTML = '<span style="color:var(--red)">预览失败: ' + esc(e.message || e) + '</span>';
  }
}

// 恢复某 agent 的路由文档为平台默认（删控制台覆盖）
async function resetCtxDocs(agent) {
  if (!confirm('恢复 ' + agent + ' 的路由文档为平台默认？（控制台的改动会清掉）')) return;
  try {
    await callCtxTool('pack.entries', { agent, reset: true });
    await loadCtxPanel();
  } catch (e) { alert('恢复失败: ' + (e.message || e)); }
}

// 在某个 agent 文件夹里新建包（owner 预填）
function newPackIn(owner) {
  window.__newPack = true;
  window.__newPackOwner = owner || '';
  window.__newPackWithAgents = !owner;   // 文件夹内新建 = 目标 agent 隐含跟随该文件夹，不再让人选
  openPackForm('');
}

// 顶部按钮：新建"通用包"（不属于任何 agent，所有 agent 都注入）
function newNamedPack() {
  newPackIn('');
}

// 包设置弹窗：按需挂到 document 最外层（不能放在 detailBody 里——切换面板会被清空）
function ensurePackSetModal() {
  let m = document.getElementById('packSetModal');
  if (m) return m;
  m = document.createElement('div');
  m.className = 'modal-mask';
  m.id = 'packSetModal';
  m.setAttribute('onclick', 'if(event.target===this)closePackSettings()');
  m.innerHTML = `<div class="modal" style="width:620px">
      <button class="close" onclick="closePackSettings()">✕</button>
      <h3>✎ 包设置</h3>
      <div id="packSetBody" style="font-size:12px"></div>
    </div>`;
  document.body.appendChild(m);
  return m;
}

// 包设置：包名 + 目标 agent（只列"已启用"的 agent 接口；已绑定但现已禁用的另标出，避免悄悄丢绑定）
function openPackSettings(id) {
  // 默认包：id 传的是 agent（其"包"就是该 agent 的默认文档清单）
  const isDocs = !!((window.__ctxAgents || []).find(a => a.agent === id) && !((window.__ctxMeta && window.__ctxMeta.packs) || []).some(p => p.id === id));
  const p = isDocs
    ? ((window.__ctxAgents || []).find(a => a.agent === id) || {})
    : ((window.__ctxMeta && window.__ctxMeta.packs) || []).find(x => x.id === id);
  if (!p) return;
  window.__packSetId = id;
  window.__packSetIsDocs = isDocs;
  const routes = window.__ctxRoutes || [];
  const enabledAgents = routes.filter(r => r.enabled).map(r => r.agent);
  const current = (p.agents || []);
  const disabledBound = current.filter(a => enabledAgents.indexOf(a) < 0);
  const box = document.getElementById('packSetBody');
  if (!box) return;
  box.innerHTML = `
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
      <span style="color:var(--dim);width:76px">包名</span>
      <input id="packSetName" value="${esc(p.name)}"
        style="flex:1;background:var(--bg);border:1px solid var(--line);color:var(--fg);border-radius:6px;padding:6px 8px">
    </div>
    <div style="display:flex;gap:8px;align-items:flex-start;margin-bottom:10px">
      <span style="color:var(--dim);width:76px;padding-top:3px">目标 agent</span>
      <div style="flex:1">
        <div style="color:var(--dim);font-size:11px;margin-bottom:6px">
          只列<b>已启用</b>的 agent 接口（禁用的不参与选择）；全勾或不勾 = 所有 agent
        </div>
        ${enabledAgents.map(a => `<label style="display:inline-block;margin:0 12px 6px 0;cursor:pointer">
            <input type="checkbox" class="packSetAgent" value="${esc(a)}"
              ${(!current.length || current.indexOf(a) >= 0) ? 'checked' : ''}> ${esc(a)}</label>`).join('')
          || '<span style="color:var(--amber)">当前没有已启用的 agent 接口（先在平台配置里打开某个 agent 的注入）</span>'}
        ${disabledBound.length ? `<div style="color:var(--amber);font-size:11px;margin-top:6px">
          已绑定但现已禁用（保留不动）：${esc(disabledBound.join('、'))}</div>` : ''}
      </div>
    </div>
    <div style="display:flex;gap:8px;align-items:center">
      <button class="act" onclick="savePackSettings()">💾 保存</button>
      <button class="act" style="background:var(--dim)" onclick="closePackSettings()">取消</button>
      <span id="packSetMsg" style="font-size:12px;color:var(--dim)"></span>
    </div>`;
  ensurePackSetModal().classList.add('show');
}

function closePackSettings() {
  const m = document.getElementById('packSetModal');
  if (m) m.classList.remove('show');
  window.__packSetId = null;
  window.__packSetIsDocs = false;
}

async function savePackSettings() {
  const id = window.__packSetId;
  const msg = document.getElementById('packSetMsg');
  if (!id) return;
  const name = (document.getElementById('packSetName')?.value || '').trim();
  if (!name) { if (msg) { msg.style.color = 'var(--red)'; msg.textContent = '包名不能为空'; } return; }
  const all = Array.from(document.querySelectorAll('.packSetAgent')).map(x => x.value);
  const picked = Array.from(document.querySelectorAll('.packSetAgent:checked')).map(x => x.value);
  // 全勾 = 所有 agent（存空数组，新增 agent 也自动带上）
  const agents = (all.length && picked.length === all.length) ? [] : picked;
  if (msg) { msg.style.color = 'var(--amber)'; msg.textContent = '保存中（插件正在重新组包）…'; }
  try {
    if (window.__packSetIsDocs) {
      // 默认包：改名落 _names（pack.entries 的 name 字段）；文件清单由卡片操作，这里不动
      await callCtxTool('pack.entries', { agent: id, name });
    } else {
      await callCtxTool('pack.update', { id, name, agents });
    }
    closePackSettings();
    await loadCtxPanel();
  } catch (e) {
    if (msg) { msg.style.color = 'var(--red)'; msg.textContent = '保存失败: ' + (e.message || e); }
  }
}

// agent 级注入开关：已并入卡片头（见 toggleCtxInject）

// ---- 命名包：新建 / 编辑 / 删除 / 挑文件（2026-09-13 加）----
// 数据与动作都在插件里（plugin.context-pack 的 pack.* 工具），这里只是控制台界面。
// 走 /api/bridge/plugins/call（控制台专用通道，平台不鉴权：本机单人使用）。
async function openPackForm(id) {
  const box = document.getElementById('packFormBox');
  if (!box) return;
  let p = { id: '', name: '', files: [], agents: [], enabled: true };
  try {
    const d = await callCtxTool('pack.list', {});
    // 只列"已启用"的 agent 接口（与包设置弹窗口径一致）
    const enabledAgents = (d.agents || []).filter(a => a.enabled).map(a => a.agent);
    window.__packAgents = enabledAgents;
    if (id) p = (d.packs || []).find(x => x.id === id) || p;   // 兼容：传 id 仍可预填
  } catch (e) {
    box.innerHTML = '<div style="color:var(--red)">取包清单失败: ' + esc(e.message || e) + '</div>';
    return;
  }
  const agents = window.__packAgents || [];
  const picked = p.agents || [];
  window.__packEdit = { id: p.id || '', files: fileEntries(p.files) };
  box.innerHTML = `
    <div style="border:1px solid var(--line);border-radius:8px;padding:12px;margin-bottom:12px">
      <div style="font-size:13px;color:var(--blue);margin-bottom:10px">＋ 新建包${window.__newPackOwner ? '（在 ' + esc(window.__newPackOwner) + ' 文件夹下）' : '（通用包：所有 agent）'}</div>
      <div style="color:var(--dim);font-size:11px;margin-bottom:8px">建好后在卡片上即时增删文件 / 单文件启停（不用再回来点保存）</div>
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
        <span style="color:var(--dim);font-size:12px;width:76px">包名</span>
        <input id="packName" value="${esc(p.name)}" placeholder="如：项目-枢阁"
          style="flex:1;background:var(--bg);border:1px solid var(--line);color:var(--fg);border-radius:6px;padding:6px 8px">
      </div>
      ${window.__newPackWithAgents
        ? `<div style="display:flex;gap:8px;align-items:flex-start;margin-bottom:8px">
            <span style="color:var(--dim);font-size:12px;width:76px;padding-top:3px">目标 agent</span>
            <div style="flex:1;font-size:12px">
              ${agents.map(a => `<label style="margin-right:12px;cursor:pointer"><input type="checkbox" class="packAgent" value="${esc(a)}"
                ${(!picked.length || picked.includes(a)) ? 'checked' : ''}> ${esc(a)}</label>`).join('')
                || '<span style="color:var(--dim)">（平台还没有已启用的 agent 接口）</span>'}
              <div style="color:var(--dim);font-size:11px">只列已启用的接口；不勾 = 通用包（所有 agent，含以后新增的）</div>
            </div>
          </div>`
        : `<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
            <span style="color:var(--dim);font-size:12px;width:76px">目标 agent</span>
            <span style="font-size:12px;color:var(--green)">${esc(window.__newPackOwner)}</span>
            <span style="color:var(--dim);font-size:11px">（跟随当前文件夹，不用选）</span>
          </div>`}
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
        <span style="color:var(--dim);font-size:12px;width:76px">注入开关</span>
        <label style="font-size:12px;cursor:pointer"><input type="checkbox" id="packEnabled" checked> 建好即启用（取消勾选 = 建好先停用）</label>
      </div>
      <div style="display:flex;gap:8px;align-items:flex-start">
        <span style="color:var(--dim);font-size:12px;width:76px;padding-top:3px">文件</span>
        <div style="flex:1">
          <div id="packFiles" style="font-size:12px;margin-bottom:6px"></div>
          <button class="mini" onclick="openVaultPicker()">📂 从库中选择文件</button>
          <button class="mini" onclick="previewPackFiles()">👀 看看内容</button>
          <button class="mini" onclick="clearPackFiles()">清空</button>
          <div id="packPreview" style="margin-top:8px;font-size:12px;max-height:220px;overflow-y:auto"></div>
        </div>
      </div>
      <div style="margin-top:12px;display:flex;gap:8px">
        <button class="act" onclick="savePack()">💾 保存</button>
        <button class="act" style="background:var(--dim)" onclick="closePackForm()">取消</button>
        <span id="packMsg" style="align-self:center;font-size:12px;color:var(--dim)"></span>
      </div>
    </div>`;
  renderPackFiles();
}

function closePackForm() {
  const box = document.getElementById('packFormBox');
  if (box) box.innerHTML = '';
  window.__packEdit = null;
}

function renderPackFiles() {
  const box = document.getElementById('packFiles');
  if (!box) return;
  const files = fileEntries(window.__packEdit && window.__packEdit.files);
  if (!files.length) {
    box.innerHTML = '<span style="color:var(--amber)">还没选文件（空包也能存，但不会有内容）</span>';
    return;
  }
  box.innerHTML = files.map((f, i) => `<div style="display:flex;gap:6px;align-items:center">
      <label style="cursor:pointer" title="取消勾选=停用（条目留着，内容不进包）">
        <input type="checkbox" ${f.enabled ? 'checked' : ''} onchange="togglePackFile(${i}, this.checked)">
      </label>
      <span style="color:${f.enabled ? 'var(--fg)' : 'var(--dim)'};${f.enabled ? '' : 'text-decoration:line-through'}">${esc(f.path)}</span>
      <button class="mini" onclick="removePackFile('${encodeURIComponent(f.path)}')">✕</button>
    </div>`).join('')
    + `<div style="color:var(--dim);font-size:11px;margin-top:4px">启用 ${files.filter(x => x.enabled).length} / 共 ${files.length}｜取消勾选=停用</div>`;
}

function togglePackFile(idx, on) {
  const files = fileEntries(window.__packEdit && window.__packEdit.files);
  if (!files[idx]) return;
  files[idx].enabled = !!on;
  window.__packEdit.files = files;
  renderPackFiles();
  if (window.__vaultFiles) renderVaultPick();
}

function removePackFile(enc) {
  if (!window.__packEdit) return;
  const p = decodeURIComponent(enc);
  window.__packEdit.files = fileEntries(window.__packEdit.files).filter(x => x.path !== p);
  renderPackFiles();
  if (window.__vaultFiles) renderVaultPick();
}

function clearPackFiles() {
  if (!window.__packEdit) return;
  window.__packEdit.files = [];
  renderPackFiles();
  if (window.__vaultFiles) renderVaultPick();
}

async function savePack() {
  const msg = document.getElementById('packMsg');
  const name = (document.getElementById('packName')?.value || '').trim();
  const rawFiles = fileEntries(window.__packEdit && window.__packEdit.files);
  const files = rawFiles.map(e => ({ path: e.path, enabled: e.enabled }));  // 显式带 enabled
  const owner = window.__newPackOwner || '';
  let agents;
  if (window.__newPackWithAgents) {
    const all = Array.from(document.querySelectorAll('.packAgent')).map(x => x.value);
    const picked = Array.from(document.querySelectorAll('.packAgent:checked')).map(x => x.value);
    // 全勾 = 所有 agent（存空数组，将来新增 agent 也自动带上）
    agents = (all.length && picked.length === all.length) ? [] : picked;
  } else {
    agents = owner ? [owner] : [];   // 文件夹内新建：目标 agent 跟随文件夹
  }
  if (!name) { msg.style.color = 'var(--red)'; msg.textContent = '包名不能为空'; return; }
  msg.style.color = 'var(--amber)'; msg.textContent = '保存中（插件正在重新组包）…';
  try {
    // 表单只负责"新建包"；已有包的编辑（文件启停/增删/改名）在卡片上即时完成
    const enabled = document.getElementById('packEnabled') ? document.getElementById('packEnabled').checked : true;
    const created = await callCtxTool('pack.create', { name, files, agents, enabled, owner });
    closePackForm();
    const newId = created && created.pack && created.pack.id;
    await loadCtxPanel();
    if (newId) {
      window.__ctxExpanded['folder:' + owner] = true;
      window.__ctxExpanded['pack:' + newId] = true;
      renderCtxBoard();
    }
  } catch (e) {
    msg.style.color = 'var(--red)';
    msg.textContent = '保存失败: ' + (e.message || e);
  }
}

async function togglePack(id, enabled) {
  try {
    await callCtxTool('pack.update', { id, enabled });
    await loadCtxPanel();
  } catch (e) { alert('切换失败: ' + (e.message || e)); }
}

async function deletePack(id) {
  if (!confirm('删除这个包？（只删清单条目，Vault 里的文件不动）')) return;
  try {
    await callCtxTool('pack.delete', { id });
    await loadCtxPanel();
  } catch (e) { alert('删除失败: ' + (e.message || e)); }
}

async function previewPackFiles() {
  const box = document.getElementById('packPreview');
  if (!box || !window.__packEdit) return;
  const files = window.__packEdit.files || [];
  if (!files.length) { box.innerHTML = '<span style="color:var(--amber)">先选文件</span>'; return; }
  box.innerHTML = '<span style="color:var(--dim)">读取中…</span>';
  try {
    const d = await callCtxTool('pack.preview', { files });
    box.innerHTML = (d.files || []).map(f => `<div style="border-left:2px solid var(--line);padding-left:8px;margin-bottom:8px">
        <div style="color:var(--blue)">${esc(f.path)}
          <span style="color:var(--dim)">${f.chars} 字符${f.ok ? '' : '｜读取失败：' + esc(f.err || '')}</span></div>
        <div style="color:var(--dim);white-space:pre-wrap">${esc((f.head || '').slice(0, 200))}</div>
      </div>`).join('') || '<span style="color:var(--dim)">没有可预览的内容</span>';
  } catch (e) {
    box.innerHTML = '<span style="color:var(--red)">预览失败: ' + esc(e.message || e) + '</span>';
  }
}

// ---- 文件选择器（弹窗；文件树 + 关键字筛选）----
// **先问库（真相源）**：GET /notes/api/tree → 转成选择器要的树形，路径形如 vault/项目/x.md。
// 库不可用 → 退回 GET /api/vault/list（磁盘），并在弹窗顶上写明来源，不让人猜。
// 交互：点文件夹展开/收起；拖文件到文件夹节点 = 移动（调 /api/vault/move，仅磁盘模式）；
//       勾选/拖到右侧「已选」= 加入当前包。

// 库的树 {name,children,files} → 选择器的树 {type,path,name,children,size}
function libTreeToPickTree(node, prefix) {
  const path = prefix ? (prefix + '/' + (node.name || '')) : (node.name || '');
  const children = [];
  for (const c of (node.children || [])) children.push(libTreeToPickTree(c, path));
  for (const f of (node.files || [])) {
    children.push({ type: 'file', name: f.name, path: path ? path + '/' + f.name : f.name,
                    size: f.size || 0, id: f.id });
  }
  return { type: 'dir', name: node.name || '', path, children };
}

async function loadVaultFileList() {
  if (window.__vaultTree) return window.__vaultTree;
  let tree = null;
  try {
    const t = await fetchJSON('/notes/api/tree');
    tree = libTreeToPickTree(t.tree || { name: '', children: [], files: [] }, '');
    window.__pickSource = '库';
  } catch (e) {
    window.__pickSource = 'Vault 磁盘（库不可用：' + ((e && e.message) || e) + '）';
    tree = await fetchJSON('/api/vault/list');
  }
  window.__vaultTree = tree;
  // **默认全收起**（辞玖 2026-10-01：「注入包模块添加文件时文件树应默认收起」）
  // 只展开根节点 —— 第一眼看到的是顶层那几个文件夹，不铺开。
  // 只初始化一次：展开/收起也会走到这里，每次都重置的话用户就永远展不开。
  if (!window.__vaultOpen) window.__vaultOpen = { '': true };
  return tree;
}

function pickerSelectedPaths() {
  return fileEntries(window.__packEdit && window.__packEdit.files).map(e => e.path);
}

function pickerIncluded(path) {
  return pickerSelectedPaths().indexOf(path) >= 0;
}

function treeRowHtml(n, depth) {
  const pad = 6 + depth * 14;
  if (n.type === 'dir') {
    // 根节点（path 为空）永远展开，否则整棵树只剩一层（实测踩到）
    const open = n.path === '' || !!(window.__vaultOpen && window.__vaultOpen[n.path]);
    return `<div data-dirpath="${esc(n.path)}" draggable="false"
        ondragover="onTreeDragOver(event, 1)"
        ondragleave="onTreeDragLeave(event)"
        ondrop="onTreeDrop(event, '${esc(n.path)}')"
        onclick="toggleVaultDir('${encodeURIComponent(n.path)}')"
        style="padding:3px 6px;padding-left:${pad}px;cursor:pointer;border-radius:4px">
        <span style="color:var(--blue)">${open ? '📂' : '📁'} ${esc(n.name)}</span>
        <span style="color:var(--dim);font-size:11px">${(n.children || []).length}</span>
      </div>`
      + (open ? (n.children || []).map(c => treeRowHtml(c, depth + 1)).join('') : '');
  }
  const on = pickerIncluded(n.path);
  return `<div draggable="true" data-path="${esc(n.path)}"
      ondragstart="onTreeDragStart(event)"
      ondragover="onTreeDragOver(event, 0)"
      ondragleave="onTreeDragLeave(event)"
      ondrop="onTreeDrop(event, '')"
      style="padding:3px 6px;padding-left:${pad}px;cursor:grab;border-radius:4px;${on ? 'background:var(--ok-bg)' : ''}">
      <label style="cursor:pointer" onclick="event.stopPropagation()">
        <input type="checkbox" data-path="${esc(n.path)}" ${on ? 'checked' : ''}
          onchange="pickToggle(this.dataset.path, this.checked)"></label>
      <span style="color:${on ? 'var(--green)' : 'var(--fg)'}">${esc(n.name)}</span>
      <span style="color:var(--dim);font-size:11px">${n.size ? (n.size / 1024).toFixed(1) + 'KB' : ''}</span>
    </div>`;
}

function renderVaultPick() {
  const box = document.getElementById('vaultPickBox');
  if (!box) return;
  const tree = window.__vaultTree;
  if (!tree) { box.innerHTML = '<div style="color:var(--dim)">加载中...</div>'; return; }
  const q = (document.getElementById('vaultPickFilter')?.value || '').trim().toLowerCase();
  const hit = (n) => {
    if (!q) return true;
    const name = (n.name || '').toLowerCase(), p = (n.path || '').toLowerCase();
    if (name.indexOf(q) >= 0 || p.indexOf(q) >= 0) return true;
    return (n.children || []).some(hit);
  };
  // 筛选时把"命中路径上的文件夹"展开：默认全收起之后，不这么做的话筛了也看不见（2026-10-01）
  if (q) {
    const openHits = (n) => {
      for (const c of (n.children || [])) {
        if (c.type === 'dir' && hit(c)) { window.__vaultOpen[c.path] = true; openHits(c); }
      }
    };
    openHits(tree);
  }
  const filtered = { name: '', path: '', type: 'dir',
    children: ((tree && tree.children) || []).filter(hit) };
  const src = '<div style="color:var(--dim);font-size:11px;margin-bottom:4px">来源：'
    + esc(window.__pickSource || '—') + '（库内路径，组包时直接取库里的正文）</div>';
  box.innerHTML = src + (treeRowHtml(filtered, 0) || '<div style="color:var(--dim)">没有匹配的文件</div>');
  renderPickedBox();
}

function renderPickedBox() {
  const box = document.getElementById('vaultPickedBox');
  if (!box) return;
  const files = fileEntries(window.__packEdit && window.__packEdit.files);
  box.innerHTML = files.length ? files.map(f => `<div style="font-size:12px;padding:2px 0">
      <span style="color:${f.enabled ? 'var(--fg)' : 'var(--dim)'};${f.enabled ? '' : 'text-decoration:line-through'}">${esc(f.path)}</span>
      ${isAbsEntry(f.path) ? `<span style="color:var(--amber);font-size:11px">外部·不随库更新</span>
        <button class="mini" onclick="adoptPicked('${encodeURIComponent(f.path)}')" title="复制进 Vault 的「附件/导入/」，改成库内路径后就会自动更新">纳入库</button>` : ''}
      <button class="mini" onclick="pickRemove('${encodeURIComponent(f.path)}')">✕</button>
    </div>`).join('')
    : '<div style="color:var(--dim);font-size:12px">（空）</div>';
}

function toggleVaultDir(enc) {
  const p = decodeURIComponent(enc);
  window.__vaultOpen = window.__vaultOpen || {};
  window.__vaultOpen[p] = !window.__vaultOpen[p];
  renderVaultPick();
}

function findTreeNode(path, node) {
  const n = node || window.__vaultTree;
  if (!n) return null;
  if (n.path === path) return n;
  for (const c of (n.children || [])) { const r = findTreeNode(path, c); if (r) return r; }
  return null;
}

// ── 拖放 ──────────────────────────────────────────────────────
window.__drag = null;

function onTreeDragStart(ev) {
  const row = ev.target.closest ? ev.target.closest('[data-path]') : null;
  const path = (row && row.dataset) ? row.dataset.path : '';
  if (!path) return;
  window.__drag = { path };
  if (ev.dataTransfer) { ev.dataTransfer.effectAllowed = 'copyMove'; ev.dataTransfer.setData('text/plain', path); }
}

function onTreeDragOver(ev, isDir) {
  if (!window.__drag) return;
  ev.preventDefault();
  if (ev.dataTransfer) ev.dataTransfer.dropEffect = isDir ? 'move' : 'copy';
  const el = ev.currentTarget;
  if (el && el.style) el.style.background = isDir ? 'var(--line)' : 'var(--ok-bg)';
}

function onTreeDragLeave(ev) {
  const el = ev.currentTarget;
  if (el && el.style) el.style.background = '';
}

async function onTreeDrop(ev, toDir) {
  ev.preventDefault();
  ev.stopPropagation();
  const el = ev.currentTarget;
  if (el && el.style) el.style.background = '';
  const drag = window.__drag || {};
  const from = drag.path || (ev.dataTransfer && ev.dataTransfer.getData('text/plain')) || '';
  window.__drag = null;
  if (!from) return;
  if (!toDir) {           // 拖到文件行 / 已选面板 = 加入当前包
    pickToggle(from, true);
    return;
  }
  const src = findTreeNode(from);
  if (src && src.type !== 'file') { alert('只能拖动文件（文件夹移动请用文件管理器）'); return; }
  if ((from.split('/').slice(0, -1).join('/')) === toDir) return;   // 已在目标目录
  if (toDir === from || toDir.indexOf(from + '/') === 0) { alert('不能把文件夹/文件移动到它自己里面'); return; }
  if (!confirm('把\n' + from + '\n移动到\n' + toDir + '\n？')) return;
  try {
    if (window.__pickSource === '库') {
      // 库模式：走库的移动接口（不能拿库内路径去动磁盘——那是两套东西）
      if (!src || !src.id) { alert('这条没有库内 id，无法移动'); return; }
      await fetchJSON('/notes/api/files/' + src.id + '/move', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: toDir })
      });
    } else {
      await fetchJSON('/api/vault/move', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ from, to: toDir })   // 后端只要 from+to（to=目标目录）
      });
    }
    window.__vaultTree = null;          // 树变了 → 重新拉
    await loadVaultFileList();
    renderVaultPick();
  } catch (e) { alert('移动失败: ' + (e.message || e)); }
}

// ── 添加文件（网页版 AI 的做法，默认路径）：选文件 → 上传 → 落 Vault → 自动进包 ──
// 一次点击、同页系统选择器、不弹终端；文件复制进 Vault 的「附件/导入/」，
// 包内引用 Vault 相对路径（Vault 仍是唯一真相源）。
function pickLocalFiles() {
  const el = document.getElementById('extFileInput');
  if (!el) { alert('页面缺少上传控件（请刷新）'); return; }
  el.value = '';       // 允许连续选同一个文件
  el.click();
}

async function onExtFilesChosen(input) {
  const files = Array.from((input && input.files) || []);
  if (!files.length) return;
  if (!window.__packEdit) { alert('先打开一个包（或表单）再用'); return; }
  const btn = document.querySelector('button[onclick="pickLocalFiles()"]');
  const oldText = btn ? btn.textContent : '';
  if (btn) btn.textContent = '上传中…';
  try {
    const fd = new FormData();
    for (const f of files) fd.append('file', f);
    const r = await fetch('/api/vault/upload', { method: 'POST', body: fd });
    const d = await r.json();
    if (!d || d.ok !== true) {
      alert('上传失败：' + ((d && d.error) || ('HTTP ' + r.status)));
      return;
    }
    for (const f of (d.files || [])) pickToggle(f.rel, true);   // 用 Vault 相对路径入清单
    renderVaultPick();
  } catch (e) {
    alert('上传失败: ' + (e.message || e));
  } finally {
    if (btn) btn.textContent = oldText;
    if (input) input.value = '';
  }
}

// 手填绝对路径（对话框不可用时的降级入口）
// 2026-09-24：先**纳入库**（复制进 Vault「附件/导入/」），清单里存库内相对路径 ——
// 绝对路径条目插件是直接读磁盘的，不随库/镜像更新（辞玖报的就是这个）。
async function addExternalPath(p) {
  const path = String(p || '').trim();
  if (!path) { alert('请输入文件绝对路径，例如 D:\\资料\\笔记.md'); return; }
  if (!window.__packEdit) { alert('先打开一个包（或表单）再用'); return; }
  try {
    const d = await fetchJSON('/api/vault/adopt', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }) });
    pickToggle(d.rel, true);
    window.__pickSource = '库';
    if (d.copied) alert('已纳入库：' + d.rel + '\n（原文件仍在原处；包内引用库内路径，以后自动更新）');
  } catch (e) {
    pickToggle(path, true);
    alert('纳入库失败，先按外部绝对路径加（**不会随库更新**）：' + ((e && e.message) || e));
  }
  const box = document.getElementById('vaultPickManual');
  if (box) box.value = '';
  renderVaultPick();
}

// 把清单里已有的"外部绝对路径"条目纳入库：成功后换成本地相对路径
async function adoptPicked(enc) {
  const old = decodeURIComponent(enc);
  try {
    const d = await fetchJSON('/api/vault/adopt', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: old }) });
    pickToggle(old, false);
    pickToggle(d.rel, true);
    alert('已纳入库：' + d.rel);
  } catch (e) {
    alert('纳入库失败：' + ((e && e.message) || e));
  }
  renderVaultPick();
}

function isAbsEntry(p) {
  const s = String(p || '');
  return /^[A-Za-z]:[\\/]/.test(s) || s.startsWith('\\\\');
}

function onPickDropList(ev) {
  ev.preventDefault();
  const drag = window.__drag || {};
  const from = drag.path || (ev.dataTransfer && ev.dataTransfer.getData('text/plain')) || '';
  window.__drag = null;
  if (from) pickToggle(from, true);
}

function pickRemove(enc) {
  pickToggle(decodeURIComponent(enc), false);
}

async function openVaultPicker() {
  if (!window.__packEdit) return;
  window.__pickBefore = fileEntries(window.__packEdit.files);
  window.__pickMode = { kind: 'pack' };
  document.getElementById('vaultPickModal').classList.add('show');
  try { await loadVaultFileList(); } catch (e) { return; }
  const f = document.getElementById('vaultPickFilter');
  if (f) { f.value = ''; setTimeout(() => f.focus(), 50); }
  renderVaultPick();
}

function pickToggle(path, on) {
  if (!window.__packEdit) return;
  const mode = window.__pickMode || { kind: 'pack' };
  const files = fileEntries(window.__packEdit.files);
  const i = files.findIndex(e => e.path === path);
  if (on && i < 0) {
    if (files.length >= PACK_MAX_FILES) { alert('最多 ' + PACK_MAX_FILES + ' 个文件'); return; }
    files.push({ path, enabled: true });
  } else if (!on && i >= 0) {
    files.splice(i, 1);
  } else {
    return;   // 无变化
  }
  window.__packEdit.files = files;
  if (mode.kind === 'docs') {
    const payload = files.map(e => ({ path: e.path, enabled: e.enabled }));
    const key = 'docs:' + mode.agent;
    window.__ctxExpanded['folder:' + mode.agent] = true;
    callCtxTool('pack.entries', { agent: mode.agent, files: payload })
      .then(() => loadCtxPanel()).then(() => renderCtxBoard())
      .catch(e => alert('保存路由文档失败: ' + (e.message || e)));
  } else if (mode.kind === 'named') {
    const payload = files.map(e => ({ path: e.path, enabled: e.enabled }));
    const key = 'pack:' + mode.id;
    window.__ctxExpanded[key] = true;
    callCtxTool('pack.update', { id: mode.id, files: payload })
      .then(() => loadCtxPanel()).then(() => renderCtxBoard())
      .catch(e => alert('保存包文件失败: ' + (e.message || e)));
  } else {
    renderPackFiles();
  }
  renderVaultPick();
}

function closeVaultPicker(ok) {
  const mode = window.__pickMode || { kind: 'pack' };
  if (!ok && mode.kind === 'pack' && window.__packEdit) {
    window.__packEdit.files = window.__pickBefore || [];   // 新建包表单：放弃改动
    renderPackFiles();
  }
  window.__pickMode = null;
  document.getElementById('vaultPickModal').classList.remove('show');
}

window.toggleCtxExpand = toggleCtxExpand;
window.toggleCtxInject = toggleCtxInject;
window.toggleCtxFile = toggleCtxFile;
window.removeCtxEntry = removeCtxEntry;
window.addCtxFile = addCtxFile;
window.previewCtxCard = previewCtxCard;
window.resetCtxDocs = resetCtxDocs;
window.newNamedPack = newNamedPack;
window.newPackIn = newPackIn;
window.toggleCtxPack = toggleCtxPack;
window.deleteCtxPack = deleteCtxPack;
window.openPackSettings = openPackSettings;
window.closePackSettings = closePackSettings;
window.savePackSettings = savePackSettings;
window.togglePackFile = togglePackFile;
window.openPackForm = openPackForm;
window.closePackForm = closePackForm;
window.savePack = savePack;
window.togglePack = togglePack;
window.deletePack = deletePack;
window.removePackFile = removePackFile;
window.clearPackFiles = clearPackFiles;
window.previewPackFiles = previewPackFiles;
window.openVaultPicker = openVaultPicker;
window.renderVaultPick = renderVaultPick;
window.pickLocalFiles = pickLocalFiles;
window.adoptPicked = adoptPicked;
window.onExtFilesChosen = onExtFilesChosen;
window.addExternalPath = addExternalPath;
window.toggleVaultDir = toggleVaultDir;
window.onTreeDragStart = onTreeDragStart;
window.onTreeDragOver = onTreeDragOver;
window.onTreeDragLeave = onTreeDragLeave;
window.onTreeDrop = onTreeDrop;
window.onPickDropList = onPickDropList;
window.pickRemove = pickRemove;
window.closeVaultPicker = closeVaultPicker;

// ---- 事件拓扑 ----
function renderTopologyPanel() {
  return `
    <h2 class="sec">🕸 事件拓扑 <button class="mini" onclick="loadTopology()">↻ 刷新</button></h2>
    <div style="color:var(--dim);font-size:12px;margin-bottom:8px">
      节点=组件/订阅者；边=产生(观测/声明)、消费(声明)、订阅关系。实时从权威源组装。
    </div>
    <div id="topoBox"><div style="color:var(--dim)">加载拓扑...</div></div>`;
}

async function loadTopology() {
  const box = document.getElementById('topoBox');
  if (!box) return;
  try {
    const t = await fetchJSON('/api/events/topology');
    const nodes = t.nodes || [], edges = t.edges || [];
    const kindLabel = { plugin: '插件', module: '模块', agent: 'Agent', subscriber: '订阅者' };
    let html = `<table style="font-size:12px"><thead><tr>
      <th>节点</th><th>种类</th><th>状态</th><th>产生（声明）</th><th>产生（观测）</th><th>订阅</th></tr></thead><tbody>`;
    for (const n of nodes) {
      const dec = n.declared || {}, obs = n.observed || {};
      const pDec = (dec.produces || []).join(', ') || '-';
      const pObs = (obs.produces || []).join(', ') || '-';
      const subs = (n.subscribes || []).join(', ') || (n.kind === 'subscriber' ? '全部事件' : '-');
      html += `<tr>
        <td>${esc(n.id)}</td>
        <td><span class="badge ${n.kind === 'subscriber' ? 'guard' : 'tool'}">${kindLabel[n.kind] || n.kind}</span></td>
        <td>${esc(n.status || '-')}</td>
        <td style="color:var(--green)">${esc(pDec)}</td>
        <td style="color:var(--cyan)">${esc(pObs)}</td>
        <td style="color:var(--amber)">${esc(subs)}</td>
      </tr>`;
    }
    html += `</tbody></table>
      <div style="color:var(--dim);font-size:12px;margin-top:6px">
        节点 ${nodes.length} 个 · 关系边 ${edges.length} 条 · 生成于 ${esc((t.generatedAt || '').replace('T', ' ').slice(0, 19))}
      </div>`;
    box.innerHTML = html;
  } catch (e) {
    box.innerHTML = '<div style="color:var(--red)">加载失败: ' + e + '</div>';
  }
}
window.loadTopology = loadTopology;

// 详情页插件动作：调 API → 提示 → 刷新当前详情
async function detailPluginAction(act, id) {
  const msg = document.getElementById('dpMsg');
  try {
    const r = await fetchJSON('/api/plugins/' + act, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id })
    });
    if (msg) { msg.textContent = (act === 'start' ? '已启用 ✓' : act === 'stop' ? '已禁用 ✓' : '已移入回收站 ✓'); }
    // 从 hash 重拉当前详情
    const h = location.hash.replace('#/', '');
    if (h.startsWith('detail/')) {
      const parts = h.split('/');
      showDetailPage(decodeURIComponent(parts[1]), decodeURIComponent(parts.slice(2).join('/')));
    }
    tickConsole();
  } catch (e) {
    if (msg) { msg.textContent = '操作失败: ' + e; }
  }
}
window.detailPluginAction = detailPluginAction;

// 事件查询面板（层级 + 关键词 + 时间）
function renderEventQueryPanel() {
  return `
    <h2 class="sec">🔍 事件查询</h2>
    <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px;align-items:center">
      <select id="eqLevel" style="background:var(--panel3);border:1px solid var(--line);color:var(--fg);padding:6px;border-radius:6px">
        <option value="platform">平台视角 (platform)</option>
        <option value="all">总账 (all)</option>
        <option value="agent">Agent 视角</option>
      </select>
      <input id="eqAgent" placeholder="agent id（如 agent.reasonix）" style="background:var(--panel3);border:1px solid var(--line);color:var(--fg);padding:6px;border-radius:6px;width:190px">
      <input id="eqKw" placeholder="关键词（如 note.search / 测试）" style="background:var(--panel3);border:1px solid var(--line);color:var(--fg);padding:6px;border-radius:6px;width:200px">
      <input id="eqType" placeholder="类型前缀（如 note.）" style="background:var(--panel3);border:1px solid var(--line);color:var(--fg);padding:6px;border-radius:6px;width:140px">
      <input id="eqFrom" type="datetime-local" style="background:var(--panel3);border:1px solid var(--line);color-scheme:dark;color:var(--fg);padding:6px;border-radius:6px">
      <span style="color:var(--dim)">至</span>
      <input id="eqTo" type="datetime-local" style="background:var(--panel3);border:1px solid var(--line);color-scheme:dark;color:var(--fg);padding:6px;border-radius:6px">
      <button class="act" onclick="doEventQuery()">查询</button>
    </div>
    <div style="color:var(--dim);font-size:12px;margin-bottom:8px">时间用本地时区选择，自动转 UTC 查询；结果按时间倒序。</div>
    <div id="eqResult"></div>`;
}

async function doEventQuery() {
  const el = document.getElementById('eqResult');
  el.innerHTML = '<div style="color:var(--dim)">查询中...</div>';
  const p = new URLSearchParams();
  p.set('level', document.getElementById('eqLevel').value || 'all');
  const agent = document.getElementById('eqAgent').value.trim();
  if (agent) p.set('agent', agent);
  const kw = document.getElementById('eqKw').value.trim();
  if (kw) p.set('keyword', kw);
  const typ = document.getElementById('eqType').value.trim();
  if (typ) p.set('type', typ);
  const f = document.getElementById('eqFrom').value, t = document.getElementById('eqTo').value;
  if (f) p.set('from', new Date(f).toISOString());
  if (t) p.set('to', new Date(t).toISOString());
  p.set('limit', '100');
  try {
    const rows = await fetchJSON('/api/events/search?' + p.toString());
    if (!rows.length) { el.innerHTML = '<div style="color:var(--dim)">无匹配记录</div>'; return; }
    el.innerHTML = `<div style="background:var(--panel2);border-radius:8px;overflow:hidden">` + rows.map(r =>
      `<div style="padding:7px 12px;border-bottom:1px solid var(--panel);font-size:12px">
        <span style="color:var(--dim);margin-right:8px">${esc((r.ts || '').replace('T', ' ').slice(0, 19))}</span>
        <span style="color:var(--cyan);margin-right:8px">${esc(r.type || '')}</span>
        <span style="color:var(--green);margin-right:8px">← ${esc(r.source || '')}</span>
        <span style="color:var(--dim)">${esc(JSON.stringify(r.payload || {}))}</span>
      </div>`).join('') + `</div>
      <div style="color:var(--dim);font-size:12px;margin-top:6px">共 ${rows.length} 条</div>`;
  } catch (e) { el.innerHTML = '<div style="color:var(--red)">查询失败: ' + e + '</div>'; }
}
window.doEventQuery = doEventQuery;

// ---- 工具 ----
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

// 组件状态 → 中文标签 + 徽章色（插件启用/禁用语义）
function statusInfo(st, kind) {
  if (kind === 'plugin') {
    if (st === 'running') return { text: '✅ 启用中', cls: 'ok' };
    if (st === 'stopped') return { text: '⛔ 已禁用', cls: 'warn' };
  }
  if (st === 'connected') return { text: '✅ 已连接', cls: 'ok' };
  if (st === 'running') return { text: '✅ 运行中', cls: 'ok' };
  if (st === 'stopped') return { text: '⛔ 已停止', cls: 'warn' };
  return { text: st || '未知', cls: 'warn' };
}

// 文件类型图标
const ICONS = { md:'📄', txt:'📄', json:'📄', pdf:'📕', png:'🖼', jpg:'🖼', jpeg:'🖼', gif:'🖼', webp:'🖼',
  svg:'🖼', zip:'📦', rar:'📦', mp4:'🎬', mp3:'🎵', exe:'⚙️', py:'🐍', go:'🔷', html:'🌐', css:'🎨', js:'⚡' };
function iconOf(n) { return n.type==='dir' ? '📁' : (ICONS[n.ext] || '📎'); }

// ---- 笔记视图：通用文件树 ----
let treeCache = { children: [] };
let currentFile = null;
// 折叠状态（path -> bool）
const collapsed = new Set();
// 首屏默认全收起（辞玖 2026-09-13：打开时所有文件夹默认收起，别一屏全铺开）
// 只做一次：展开/收起也会调 loadTree()，若每次都重置，用户就永远展不开了
let treeCollapsedOnce = false;

// 递归收集所有文件夹路径（根节点自身不算——根的直接子级要看得见）
function collapseAllDirs(node) {
  for (const child of node.children || []) {
    if (child.type !== 'dir') continue;
    collapsed.add(child.path);
    collapseAllDirs(child);
  }
}

async function loadTree() {
  try {
    const tree = await fetchJSON('/api/vault/list');
    treeCache = tree;
    if (!treeCollapsedOnce) {   // 首屏默认全收起；之后记住用户展开过的状态
      collapseAllDirs(tree);
      treeCollapsedOnce = true;
    }
    const box = document.getElementById('fileTree');
    box.innerHTML = renderNode(tree, 0);
    box.querySelectorAll('.tree-row.dir').forEach(el => {
      el.addEventListener('click', () => toggleDir(el.dataset.path, el));
    });
    box.querySelectorAll('.tree-row.file').forEach(el => {
      el.addEventListener('click', () => openNode(el));
    });
    // 拖放支持
    setupDragDrop(box);
  } catch (e) {
    document.getElementById('fileTree').innerHTML = '<div class="empty" style="padding:20px">加载失败：' + esc(e.message) + '</div>';
  }
}

// ================= 拖放：移动文件/文件夹 =================
let dragPath = null;
let dragIsDir = false;
let hoverTimer = null;      // 悬停展开计时器
let scrollRaf = null;       // 边缘自动滚动

function setupDragDrop(box) {
  // 所有行可拖
  box.querySelectorAll('.tree-row').forEach(row => {
    row.draggable = true;
    row.addEventListener('dragstart', e => {
      dragPath = row.dataset.path;
      dragIsDir = row.classList.contains('dir');
      row.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', dragPath);
    });
    row.addEventListener('dragend', () => {
      dragPath = null;
      dragIsDir = false;
      row.classList.remove('dragging');
      clearHover();
      stopAutoScroll();
      document.querySelectorAll('.tree-row.drop-target').forEach(x => x.classList.remove('drop-target'));
      document.querySelectorAll('.tree-row.merge-target').forEach(x => x.classList.remove('merge-target'));
      hideMergeBubble();
      box.classList.remove('drag-over');
    });
    // 文件夹行：悬停 600ms 展开（闭合时）
    if (row.classList.contains('dir')) {
      row.addEventListener('dragover', e => {
        e.preventDefault();
        e.stopPropagation();
        if (!dragPath || isInvalidTarget(dragPath, row.dataset.path, true)) return;
        highlight(row);
        const isCollapsed = row.querySelector('.tw').textContent === '▸';
        if (isCollapsed && !hoverTimer) {
          hoverTimer = setTimeout(() => {
            toggleDir(row.dataset.path, row);
            hoverTimer = null;
          }, 600); // 悬停 0.6s 展开闭合文件夹
        }
      });
      row.addEventListener('dragleave', e => {
        // 仍在行内子元素间移动（图标→文字）不视为离开——防闪烁
        if (row.contains(e.relatedTarget)) return;
        row.classList.remove('drop-target');
        clearHover();
      });
      row.addEventListener('drop', e => {
        e.preventDefault();
        e.stopPropagation();
        clearHover();
        if (!dragPath || isInvalidTarget(dragPath, row.dataset.path, true)) return;
        doMove(dragPath, row.dataset.path);
      });
    }
    // 文件行：可作 drop 目标 = 移到同目录（父目录），视觉上等价移入该文件夹无意义 → 忽略文件行 drop，但允许 drop 到空白（根）
    // 文件行：悬停 = 合并预览动画（拟合成文件夹），drop = 执行合并
    if (!row.classList.contains('dir')) {
      row.addEventListener('dragover', e => {
        e.preventDefault();
        e.stopPropagation();
        if (!dragPath || dragPath === row.dataset.path || dragIsDir) return; // 拖目录到文件不合并
        const targetDir = row.dataset.path.includes('/')
          ? row.dataset.path.slice(0, row.dataset.path.lastIndexOf('/')) : '';
        const fromDir = dragPath.includes('/')
          ? dragPath.slice(0, dragPath.lastIndexOf('/')) : '';
        if (fromDir !== targetDir) return; // 只合并同目录文件
        // 已激活则不重复添加（防动画重启闪烁）；切到别的行才换
        if (!row.classList.contains('merge-target')) {
          document.querySelectorAll('.tree-row.merge-target').forEach(x => x.classList.remove('merge-target'));
          row.classList.add('merge-target');
          showMergeBubble(e.clientX, e.clientY, dragPath.split('/').pop(), row.dataset.path.split('/').pop());
        } else {
          moveMergeBubble(e.clientX, e.clientY);
        }
      });
      row.addEventListener('dragleave', e => {
        if (row.contains(e.relatedTarget)) return; // 行内移动不取消
        row.classList.remove('merge-target');
        hideMergeBubble();
      });
      row.addEventListener('drop', e => {
        e.preventDefault();
        e.stopPropagation();
        row.classList.remove('merge-target');
        hideMergeBubble();
        if (!dragPath || dragPath === row.dataset.path || dragIsDir) return;
        const targetDir = row.dataset.path.includes('/')
          ? row.dataset.path.slice(0, row.dataset.path.lastIndexOf('/')) : '';
        const fromDir = dragPath.includes('/')
          ? dragPath.slice(0, dragPath.lastIndexOf('/')) : '';
        if (fromDir !== targetDir) return;
        doMerge(dragPath, row.dataset.path);
      });
    }
  });
  setupTreeBoxEvents(box); // box 级事件只绑一次（内部防重）
}

// box 级事件（dragover/drop/dragleave/contextmenu）：一次性绑定，防累积
function setupTreeBoxEvents(box) {
  if (box.__eventsBound) return;
  box.__eventsBound = true;
  box.addEventListener('dragover', e => {
    e.preventDefault();
    if (dragPath) box.classList.add('drag-over');
    handleEdgeScroll(e);
  });
  box.addEventListener('drop', e => {
    e.preventDefault();
    box.classList.remove('drag-over');
    stopAutoScroll();
    if (dragPath && !e.target.closest('.tree-row')) {
      doMove(dragPath, ''); // 落到空白 = 移到根
    }
    clearHover();
  });
  box.addEventListener('dragleave', e => {
    if (!box.contains(e.relatedTarget)) box.classList.remove('drag-over');
  });
  box.addEventListener('contextmenu', e => {
    e.preventDefault();
    e.stopPropagation(); // 阻止冒泡到 document，避免新菜单被全局监听立即关闭
    const row = e.target.closest('.tree-row');
    showContextMenu(e.clientX, e.clientY, row ? {
      path: row.dataset.path,
      isDir: row.classList.contains('dir')
    } : null);
  });
}

function highlight(row) {
  document.querySelectorAll('.tree-row.drop-target').forEach(x => x.classList.remove('drop-target'));
  row.classList.add('drop-target');
}

// 判断 drop 目标是否无效（拖到自己 / 拖回自己所在目录）
function isInvalidTarget(from, targetPath, targetIsDir) {
  if (!from || from === targetPath) return true;          // 拖到自己
  if (!targetIsDir) {
    // 目标是文件：目标目录 = 其父目录，若等于 from 的父目录则原地
    return false; // 文件行 drop 语义是"同目录"？见 drop handler，那里再判
  }
  // 目标是目录：若 from 就在该目录下（拖回原文件夹）→ 无效（原地）
  const fromDir = from.includes('/') ? from.slice(0, from.lastIndexOf('/')) : '';
  return fromDir === targetPath;
}

function clearHover() {
  if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
}

// 边缘自动滚动：拖到容器顶/底 12% 区域时平滑滚动
function handleEdgeScroll(e) {
  const box = document.getElementById('fileTree');
  const rect = box.getBoundingClientRect();
  const zone = Math.max(40, rect.height * 0.12);
  const y = e.clientY - rect.top;
  if (y < zone) {
    startAutoScroll(box, -Math.round((zone - y) / 3 + 4)); // 向上，速度随距离
  } else if (y > rect.height - zone) {
    startAutoScroll(box, Math.round((y - (rect.height - zone)) / 3 + 4)); // 向下
  } else {
    stopAutoScroll();
  }
}

function startAutoScroll(box, speed) {
  if (scrollRaf) return;
  function step() {
    box.scrollTop += speed;
    scrollRaf = requestAnimationFrame(step);
  }
  scrollRaf = requestAnimationFrame(step);
}

function stopAutoScroll() {
  if (scrollRaf) { cancelAnimationFrame(scrollRaf); scrollRaf = null; }
}

// 执行移动
async function doMove(from, toDir) {
  try {
    const res = await fetchJSON('/api/vault/move', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ from, to: toDir }),
    });
    await loadTree(); // 刷新树
  } catch (err) {
    alert('移动失败: ' + err.message);
  }
}

// 执行合并：两文件 → 新建文件夹，随后自动进入改名
async function doMerge(aPath, bPath) {
  try {
    const res = await fetchJSON('/api/vault/merge', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ a: aPath, b: bPath }),
    });
    // 确保新目录的父目录展开（否则行不渲染，改名找不到）
    const parentDir = res.newDir.includes('/')
      ? res.newDir.slice(0, res.newDir.lastIndexOf('/')) : '';
    collapsed.delete(parentDir);
    await loadTree();
    startInlineRename(res.newDir); // 预改名
  } catch (err) {
    alert('合并失败: ' + err.message);
  }
}

// 行内改名：把目录行的名字换成输入框（自动全选），回车/失焦提交，Esc 取消
async function startInlineRename(dirPath) {
  // 兼容 data-path 的尾部斜杠（dir 树里存 "xxx/"）
  let row = document.querySelector(`.tree-row.dir[data-path="${CSS.escape(dirPath)}"]`);
  if (!row) row = document.querySelector(`.tree-row.dir[data-path="${CSS.escape(dirPath + '/')}"]`);
  if (!row) { console.warn('rename row not found:', dirPath); return; }
  const nm = row.querySelector('.nm');
  if (!nm) return;
  const oldName = nm.textContent;
  const input = document.createElement('input');
  input.value = oldName;
  input.className = 'rename-input';
  input.style.cssText = 'background:var(--panel);border:1px solid var(--blue);color:var(--fg);border-radius:4px;padding:0 6px;font-size:13px;width:100%;min-width:60px';
  nm.replaceWith(input);
  input.focus();
  input.select();

  let finished = false;
  async function commit() {
    if (finished) return;
    finished = true;
    const newName = input.value.trim();
    if (newName && newName !== oldName) {
      try {
        await fetchJSON('/api/vault/rename', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: dirPath, newName }),
        });
      } catch (e) {
        alert('重命名失败: ' + e.message);
      }
    }
    await loadTree();
  }
  function cancel() {
    if (finished) return;
    finished = true;
    loadTree();
  }
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') commit();
    if (e.key === 'Escape') cancel();
  });
  input.addEventListener('blur', commit);
}

// 合并预览气泡（只创建一次，之后跟随鼠标移动）
let mergeBubble = null;
function showMergeBubble(x, y, aName, bName) {
  hideMergeBubble();
  mergeBubble = document.createElement('div');
  mergeBubble.className = 'merge-bubble';
  mergeBubble.innerHTML = `📁 松开合并：${escapeHtml(aName)} + ${escapeHtml(bName)}`;
  moveMergeBubble(x, y);
  document.body.appendChild(mergeBubble);
}
function moveMergeBubble(x, y) {
  if (!mergeBubble) return;
  mergeBubble.style.left = (x + 14) + 'px';
  mergeBubble.style.top = (y + 14) + 'px';
}
function hideMergeBubble() {
  if (mergeBubble) { mergeBubble.remove(); mergeBubble = null; }
}
function escapeHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function renderNode(node, depth) {
  if (!node.children || node.children.length === 0) return '';
  let html = '<div class="tree-children' + (depth > 0 && collapsed.has(node.path) ? ' collapsed' : '') + '">';
  for (const child of node.children) {
    if (child.type === 'dir') {
      const isCollapsed = collapsed.has(child.path);
      html += `<div class="tree-row dir" data-path="${esc(child.path)}">
        <span class="tw">${isCollapsed ? '▸' : '▾'}</span><span class="ic">📁</span><span class="nm">${esc(child.name)}</span></div>`;
      if (!isCollapsed) {
        html += renderNode(child, depth + 1);
      }
    } else {
      html += `<div class="tree-row file" data-path="${esc(child.path)}" data-type="file">
        <span class="tw"></span><span class="ic">${iconOf(child)}</span><span class="nm">${esc(child.name)}</span></div>`;
    }
  }
  html += '</div>';
  return html;
}

function toggleDir(path, el) {
  if (collapsed.has(path)) collapsed.delete(path); else collapsed.add(path);
  loadTree(); // 简单：重渲染
}

// 打开文件节点：按类型预览
async function openNode(el) {
  document.querySelectorAll('.tree-row.file').forEach(x => x.classList.remove('active'));
  el.classList.add('active');
  const path = el.dataset.path;
  currentFile = path;
  const pane = document.getElementById('editorPane');
  pane.innerHTML = '<div class="empty">加载 ' + esc(path) + ' ...</div>';
  try {
    const data = await fetchJSON('/api/vault/read?path=' + encodeURIComponent(path));
    renderContent(pane, data);
  } catch (e) {
    pane.innerHTML = '<div class="empty">读取失败：' + esc(e.message) + '</div>';
  }
}

function renderContent(pane, data) {
  if (data.kind === 'text' && data.ext === 'md') {
    pane.innerHTML = `<div class="md-body">${mdRender(data.content)}</div>`;
    // 双链跳转
    pane.querySelectorAll('a.wiki').forEach(a => {
      a.addEventListener('click', function () {
        openWikiLink(this.dataset.target);
      });
    });
  } else if (data.kind === 'text') {
    pane.innerHTML = `<pre style="background:var(--panel3);padding:16px;border-radius:8px;overflow:auto;white-space:pre-wrap;font-size:13px">${esc(data.content)}</pre>`;
  } else if (data.kind === 'image') {
    pane.innerHTML = `<div style="text-align:center;padding:16px"><img src="${data.dataUrl}" style="max-width:100%;border-radius:8px" alt="${esc(data.name)}"><div style="color:var(--dim);margin-top:8px;font-size:12px">${esc(data.name)}</div></div>`;
  } else {
    // 二进制：下载
    pane.innerHTML = `<div class="empty">
      <div style="font-size:40px;margin-bottom:12px">📦</div>
      <div>${esc(data.name)}（${data.ext || '未知'}，${(data.size/1024).toFixed(1)} KB）</div>
      <div style="margin-top:12px"><a class="act-link" href="/api/vault/download?path=${encodeURIComponent(data.path)}">⬇ 下载文件</a></div>
    </div>`;
  }
  pane.scrollTop = 0;
}

// 双链跳转：在树里找目标文件
async function openWikiLink(target) {
  const clean = target.replace(/\.md$/, '');
  const tree = treeCache.children || [];
  const flat = [];
  (function walk(nodes) {
    for (const n of nodes) {
      if (n.type === 'file') flat.push(n);
      if (n.children) walk(n.children);
    }
  })(tree);
  const match = flat.find(f =>
    f.path === target || f.path.endsWith('/' + target + '.md') ||
    f.name === target + '.md' || f.name.replace(/\.md$/, '') === clean);
  if (match) {
    const el = document.querySelector(`.tree-row.file[data-path="${CSS.escape(match.path)}"]`);
    if (el) openNode(el);
    else { openByPath(match.path); }
  } else {
    alert('找不到笔记: ' + target);
  }
}

async function openByPath(path) {
  const data = await fetchJSON('/api/vault/read?path=' + encodeURIComponent(path));
  const pane = document.getElementById('editorPane');
  pane.innerHTML = `<div class="md-body">${mdRender(data.content)}</div>`;
  currentFile = path;
}

window.go = go;

// ---- 控制台视图 ----
async function refreshStatus() {
  const s = await fetchJSON('/api/status');
  document.getElementById('nPlugins').textContent = s.plugins;
  document.getElementById('nSubs').textContent = s.subs;
  document.getElementById('nProcs').textContent = s.processes;
}

async function refreshPlugins() {
  const list = await fetchJSON('/api/components');
  const kindLabel = { plugin: '插件', module: '模块', agent: 'Agent', system: '系统' };
  const badgeColor = { plugin: 'tool', module: 'auto', agent: 'guard', system: 'warn' };
  document.getElementById('pluginRows').innerHTML = list.map(p => {
    const si = statusInfo(p.status, p.kind);
    const descTip = p.desc ? esc(p.desc) : "";
    const descMark = p.desc ? " ⓘ" : "";
    const agentMark = p.agent_id ? ` <span style="color:var(--blue);font-size:11px">· 代理 ${esc(p.agent_id)}</span>` : "";
    return `
    <tr class="row-click" onclick="showPluginDetail('${p.kind}','${p.id}')">
      <td><span class="badge ${badgeColor[p.kind] || 'tool'}">${kindLabel[p.kind] || p.kind}</span></td>
      <td title="${descTip}">${p.id}${descMark}${agentMark}</td><td><span class="badge ${p.type}">${p.type}</span></td>
      <td>${(p.capabilities || []).join(', ') || '-'}</td>
      <td><span class="badge ${si.cls}">${si.text}</span></td>
      <td>${p.perm_level ? `<span class="badge tool" title="软件操作权限档">🔐 ${esc(p.perm_level)}</span> ` : ''}<button class="mini" onclick="event.stopPropagation();goDetail('${p.kind}','${p.id}')">详情 →</button></td>
    </tr>`;
  }).join('')
    || '<tr><td colspan="6" style="color:var(--dim)">无组件</td></tr>';
}

// 组件详情弹层（插件走 detail API；模块/Agent 用列表数据渲染）
async function showPluginDetail(kind, id) {
  let d;
  if (kind === 'plugin') {
    d = await fetchJSON('/api/plugins/detail?id=' + encodeURIComponent(id));
  } else {
    // module / agent：从统一列表找
    const list = await fetchJSON('/api/components');
    d = list.find(x => x.kind === kind && x.id === id) || { id };
  }
  document.getElementById('pmTitle').textContent = `${kind === 'plugin' ? '插件' : kind === 'module' ? '模块' : 'Agent'}详情 · ${d.name || id}`;
  const rows = [
    ['ID', d.id],
    ['种类', kind],
    ['中文名', d.name || '-'],
    ['类型', d.type],
    ['状态', d.status],
    ['能力', (d.capabilities || []).join(', ') || '-'],
  ];
  if (d.desc) rows.push(['作用', d.desc]);
  if (d.agent_id) rows.push(['代理身份', d.agent_id + (d.agent_name ? '（' + d.agent_name + '）' : '')]);
  let html = rows.map(([k, v]) =>
    `<div class="row"><span class="k">${k}</span><span class="v">${v || '-'}</span></div>`).join('');
  if (kind === 'plugin' && d.process) {
    html += `<div class="row"><span class="k">进程</span><span class="v">${
      d.process.running ? '运行中' : '已停止'}</span></div>`;
    if (d.process.restarts !== undefined)
      html += `<div class="row"><span class="k">重启次数</span><span class="v">${d.process.restarts}</span></div>`;
    if (d.process.stopped !== undefined)
      html += `<div class="row"><span class="k">主动停止</span><span class="v">${d.process.stopped ? '是' : '否'}</span></div>`;
  } else if (kind === 'plugin') {
    html += `<div class="row"><span class="k">进程</span><span class="v">builtin（同进程）</span></div>`;
  }
  // 插件操作按钮（会话级；模块/agent 暂不提供）
  if (kind === 'plugin') {
    const st = d.status || 'running';
    html += `<div style="margin-top:14px;display:flex;gap:8px">
      ${st !== 'running' ? `<button class="act" onclick="pluginAction('start','${d.id}')">▶ 启用</button>` : `<button class="act" style="background:var(--dim)" onclick="pluginAction('stop','${d.id}')">⏸ 禁用</button>`}
      <button class="act" style="background:var(--red)" onclick="pluginAction('delete','${d.id}')">🗑 删除（重启恢复）</button>
      <span id="pmActionMsg" style="align-self:center;font-size:12px;color:var(--dim)"></span>
    </div>`;
  }
  document.getElementById('pmBody').innerHTML = html;
  document.getElementById('pluginModal').classList.add('show');
}

// 插件启停/删除动作
async function pluginAction(act, id) {
  const msg = document.getElementById('pmActionMsg');
  try {
    const r = await fetchJSON('/api/plugins/' + act, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id })
    });
    if (msg) { msg.textContent = (act === 'start' ? '已启用' : act === 'stop' ? '已禁用' : '已删除') + ' ✓'; }
    closePluginModal();
    await tickConsole();
  } catch (e) {
    if (msg) { msg.textContent = '操作失败: ' + e; }
  }
}
window.pluginAction = pluginAction;
function closePluginModal() { document.getElementById('pluginModal').classList.remove('show'); }
window.showPluginDetail = showPluginDetail;
window.closePluginModal = closePluginModal;

async function refreshEvents() {
  const box = document.getElementById('eventList');
  if (!box) return; // 事件流已迁到日志视图（#/logs），控制台首页不再渲染
  const evs = await fetchJSON('/api/events');
  box.innerHTML = evs.slice().reverse().map(e =>
    `<div class="ev"><span class="t">${e.time}</span><span class="type">${e.type}</span><span class="src">← ${e.source}</span></div>`).join('')
    || '<div style="color:var(--dim)">暂无事件</div>';
}

async function publishEvent() {
  const title = document.getElementById('note').value || '控制台测试';
  await fetchJSON('/api/publish', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ type: 'note.saved', source: 'console', payload: { title } }) });
}
window.publishEvent = publishEvent;

// ---- 事件驱动（SSE）：替代轮询，插件/模块增删、事件即时刷新 ----
function connectEventStream() {
  if (window.__evtSrc) return;
  const es = new EventSource('/api/events/stream');
  window.__evtSrc = es;
  es.onopen = () => {
    // 连接恢复：若日志详情页开着实时账本，补一次全量
    if (ledgerActive()) reloadLedger();
  };
  es.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    // 组件变更：刷新组件列表（插件/模块/agent 增删自动出现/消失）
    if (ev.type && (ev.type.startsWith('plugin.') || ev.type.startsWith('component.') ||
                    ev.type.startsWith('module.') || ev.type.startsWith('agent.'))) {
      const v = location.hash.replace('#/', '') || 'notes';
      if (v === 'console') refreshPlugins();
      return;
    }
    // 事件日志/总线详情页：实时账本框
    if (ledgerActive()) appendLedger(ev);
    // 其他事件：刷新事件流
    const v2 = location.hash.replace('#/', '') || 'notes';
    if (v2 === 'console') refreshEvents();
  };
  es.onerror = () => {
    // EventSource 自动重连；连接恢复后补一次全量
    es.close();
    window.__evtSrc = null;
    setTimeout(connectEventStream, 3000);
    const v = location.hash.replace('#/', '') || 'notes';
    if (v === 'console') { refreshPlugins(); refreshEvents(); }
  };
}

// ---- 实时账本框（日志/事件总线详情页） ----
function ledgerActive() {
  return !!(document.getElementById('ledgerBox') && window.__ledgerOpen);
}

// 渲染实时账本面板（含层级选择 + 内容框）
function renderLedgerPanel(html) {
  return html + `
    <h2 class="sec">📒 账本实时内容 <span id="ledgerLive" style="font-size:11px;color:var(--dim)">● 连接中</span></h2>
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
      <select id="ledgerLevel" onchange="reloadLedger()" style="background:var(--panel3);border:1px solid var(--line);color:var(--fg);padding:5px;border-radius:6px">
        <option value="platform">平台视角 platform ★</option>
        <option value="all">总账 all（全量）</option>
        <option value="agent">Agent 视角 agent</option>
      </select>
      <input id="ledgerAgent" placeholder="agent id（agent 级时）" onchange="reloadLedger()" oninput="reloadLedger()" style="background:var(--panel3);border:1px solid var(--line);color:var(--fg);padding:5px;border-radius:6px;width:170px">
      <button class="mini" onclick="reloadLedger()">↻ 刷新</button>
      <span style="color:var(--dim);font-size:12px">实时推送中（最近 ${window.__LEDGER_LIMIT || 60} 条）</span>
    </div>
    <div id="ledgerBox" style="background:var(--bg);border:1px solid var(--panel3);border-radius:8px;padding:8px 10px;font-family:monospace;font-size:11px;max-height:340px;overflow-y:auto;line-height:1.6">
      <div style="color:var(--dim)">加载账本...</div>
    </div>`;
}

// 按层级重新拉取账本
async function reloadLedger() {
  const box = document.getElementById('ledgerBox');
  if (!box) return;
  const live = document.getElementById('ledgerLive');
  window.__ledgerOpen = true;
  if (live) live.textContent = '● 拉取中';
  const p = new URLSearchParams();
  p.set('level', document.getElementById('ledgerLevel').value || 'all');
  const agent = document.getElementById('ledgerAgent').value.trim();
  if (agent) p.set('agent', agent);
  p.set('limit', String(window.__LEDGER_LIMIT || 60));
  try {
    const rows = await fetchJSON('/api/events/search?' + p.toString());
    box.innerHTML = rows.map(r => ledgerRow(r)).join('')
      || '<div style="color:var(--dim)">账本为空</div>';
    if (live) live.textContent = '● 实时';
  } catch (e) {
    box.innerHTML = '<div style="color:var(--red)">拉取失败: ' + e + '</div>';
    if (live) live.textContent = '● 离线';
  }
}
window.reloadLedger = reloadLedger;

// 事件影响面（与后端 scope 一致）：platform / agent
function evScope(ev) {
  if (ev.scope && ev.scope.length) return ev.scope;
  return ["platform"]; // 旧事件视为平台事件
}
function touchesPlatform(ev) { return evScope(ev).includes("platform"); }
function touchesAgent(ev) { return evScope(ev).includes("agent"); }

// SSE 收到新事件 → 追加到实时账本（按当前层级过滤，最新在顶）
function appendLedger(ev) {
  const box = document.getElementById('ledgerBox');
  if (!box || !window.__ledgerOpen) return;
  const level = ((document.getElementById('ledgerLevel') || {}).value || 'platform').trim();
  const agent = ((document.getElementById('ledgerAgent') || {}).value || '').trim();
  // 层级过滤（按影响面 scope）
  if (level === 'platform' && !touchesPlatform(ev)) return;
  if (level === 'agent' && !touchesAgent(ev)) return;
  if (level === 'agent' && agent) {
    const owner = (ev.payload && ev.payload.agent) || ev.source || '';
    if (owner !== agent) return;
  }
  box.insertAdjacentHTML('afterbegin', ledgerRow(ev));
  // 限行数
  const limit = window.__LEDGER_LIMIT || 60;
  while (box.children.length > limit) box.removeChild(box.lastChild);
}
window.appendLedger = appendLedger;

function ledgerRow(ev) {
  const ts = (ev.ts || '').replace('T', ' ').slice(0, 19);
  const pay = ev.payload ? JSON.stringify(ev.payload) : '';
  return `<div style="border-bottom:1px solid var(--panel);padding:1px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
    <span style="color:var(--dim);margin-right:6px">${esc(ts)}</span>
    <span style="color:var(--cyan);margin-right:6px">${esc(ev.type || '')}</span>
    <span style="color:var(--green);margin-right:6px">← ${esc(ev.source || '')}</span>
    <span style="color:var(--fg3)">${esc(pay)}</span>
  </div>`;
}

// ---- 插件回收站 ----
async function openPluginTrash() {
  const box = document.getElementById('pluginTrashRows');
  document.getElementById('pluginTrashModal').classList.add('show');
  box.innerHTML = '<div style="color:var(--dim)">加载中...</div>';
  try {
    const list = await fetchJSON('/api/plugins/trash');
    box.innerHTML = !list.length ? '<div style="color:var(--dim)">回收站是空的</div>' : `
      <table><thead><tr><th style="width:30px"></th><th>ID</th><th>类型</th><th>删除时间</th></tr></thead><tbody>
      ${list.map(t => `<tr>
        <td><input type="checkbox" value="${esc(t.id)}" style="accent-color:var(--blue)"></td>
        <td>${esc(t.name || t.id)}</td><td>${esc(t.type)}</td>
        <td style="color:var(--dim)">${esc((t.deletedAt || '').replace('T',' ').slice(0,19))}</td>
      </tr>`).join('')}</tbody></table>
      <div style="color:var(--dim);font-size:12px;margin-top:6px">共 ${list.length} 项</div>`;
  } catch (e) { box.innerHTML = '<div style="color:var(--red)">加载失败: ' + e + '</div>'; }
}
function closePluginTrash() { document.getElementById('pluginTrashModal').classList.remove('show'); }
function trashSelectedIds() {
  return [...document.querySelectorAll('#pluginTrashRows input:checked')].map(x => x.value);
}
async function pluginTrashRestoreSel() {
  const ids = trashSelectedIds();
  if (!ids.length) { alert('请先勾选要恢复的插件'); return; }
  await fetchJSON('/api/plugins/trash/restore', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids }) });
  await openPluginTrash(); refreshPlugins();
}
async function pluginTrashPurgeSel() {
  const ids = trashSelectedIds();
  if (!ids.length) { alert('请先勾选要永久删除的插件'); return; }
  if (!confirm('永久删除后不可恢复，确定删除 ' + ids.join(', ') + ' ？（重启后不再出现）')) return;
  await fetchJSON('/api/plugins/trash/purge', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids }) });
  await openPluginTrash(); refreshPlugins();
}
window.openPluginTrash = openPluginTrash;
window.closePluginTrash = closePluginTrash;
window.pluginTrashRestoreSel = pluginTrashRestoreSel;
window.pluginTrashPurgeSel = pluginTrashPurgeSel;

// ---- 轮询 ----
async function tickConsole() {
  try { await Promise.all([refreshStatus(), refreshPlugins(), refreshEvents()]); }
  catch (e) { /* 静默 */ }
}

async function ping() {
  const pill = document.getElementById('connPill');
  try { await fetchJSON('/api/status'); pill.textContent = '● 已连接'; pill.style.color = 'var(--green)'; }
  catch (e) { pill.textContent = '○ 离线'; pill.style.color = 'var(--red)'; }
}

// 初始化（默认落在「内核」这一页：库与整体生命都在控制台那层，见 page.go）
const init = location.hash.replace('#/', '') || 'console';
go(init);
ping();
connectEventStream();
setInterval(ping, 5000);
setInterval(() => {
  const v = location.hash.replace('#/', '') || 'console';
  if (v === 'console') { refreshStatus(); }
}, 5000);

// ================= 右键菜单 =================
// target: null=空白(根) | {path,isDir} 行上
function showContextMenu(x, y, target) {
  hideContextMenu();
  const menu = document.createElement('div');
  menu.className = 'ctx-menu';
  const items = [];
  const add = (label, fn, danger) => items.push({ label, fn, danger });

  if (!target) {
    // 空白：在根目录新建
    add('📁 新建文件夹', () => createDirThenRename(''));
    add('📄 新建笔记', () => createFileThenOpen('', null));
  } else if (target.isDir) {
    const dir = target.path.replace(/\/$/, '');
    add('📁 新建文件夹', () => createDirThenRename(dir));
    add('📄 新建笔记', () => createFileThenOpen(dir, null));
    add('✏️ 重命名', () => startInlineRename(dir));
    add('📂 取消文件夹（内容释放到上级）', () => unfolderDir(dir));
    add('🗑 删除', () => deleteNode(target.path), true);
  } else {
    // 文件
    const p = target.path;
    const dir = p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '';
    add('📄 新建笔记', () => createFileThenOpen(dir, null));
    add('✏️ 重命名', () => { startInlineRenameFile(p); });
    add('⬇ 下载', () => { window.open('/api/vault/download?path=' + encodeURIComponent(p), '_blank'); });
    add('🗑 删除', () => deleteNode(p), true);
  }

  for (const it of items) {
    const el = document.createElement('div');
    el.className = 'item' + (it.danger ? ' danger' : '');
    el.innerHTML = it.label;
    el.addEventListener('click', () => { hideContextMenu(); it.fn(); });
    menu.appendChild(el);
  }
  // 边界防溢出
  const mw = 160, mh = items.length * 34 + 10;
  menu.style.left = Math.min(x, window.innerWidth - mw - 8) + 'px';
  menu.style.top = Math.min(y, window.innerHeight - mh - 8) + 'px';
  document.body.appendChild(menu);
  ensureGlobalMenuClose(); // 全局关闭监听只注册一次
}

// 全局菜单关闭：点击/右键在菜单外 → 关闭（只在首次调用时注册）
let _menuCloseBound = false;
function ensureGlobalMenuClose() {
  if (_menuCloseBound) return;
  _menuCloseBound = true;
  document.addEventListener('click', e => {
    if (!e.target.closest('.ctx-menu')) hideContextMenu();
  });
  document.addEventListener('contextmenu', e => {
    if (!e.target.closest('.ctx-menu')) hideContextMenu();
  });
}

function hideContextMenu() {
  document.querySelectorAll('.ctx-menu').forEach(m => m.remove());
}

// 新建文件夹 → 展开父 → 自动预改名
async function createDirThenRename(parentDir) {
  try {
    const res = await fetchJSON('/api/vault/new-dir', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ parent: parentDir }),
    });
    if (parentDir) collapsed.delete(parentDir);
    await loadTree();
    startInlineRename(res.newDir);
  } catch (e) { alert('新建文件夹失败: ' + e.message); }
}

// 新建 md 文件 → 打开编辑/预览
async function createFileThenOpen(dir, name) {
  try {
    const res = await fetchJSON('/api/vault/new-file', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dir, name }),
    });
    await loadTree();
    // 自动打开新文件预览（后续做编辑器后可进编辑态）
    const data = await fetchJSON('/api/vault/read?path=' + encodeURIComponent(res.newFile));
    const pane = document.getElementById('editorPane');
    pane.innerHTML = `<div class="md-body">${mdRender(data.content)}</div>`;
  } catch (e) { alert('新建笔记失败: ' + e.message); }
}

// 文件行内改名
async function startInlineRenameFile(filePath) {
  const row = document.querySelector(`.tree-row.file[data-path="${CSS.escape(filePath)}"]`);
  if (!row) return;
  const nm = row.querySelector('.nm');
  if (!nm) return;
  const oldName = nm.textContent;
  const input = document.createElement('input');
  input.value = oldName;
  input.className = 'rename-input';
  input.style.cssText = 'background:var(--panel);border:1px solid var(--blue);color:var(--fg);border-radius:4px;padding:0 6px;font-size:13px;width:100%;min-width:60px';
  nm.replaceWith(input);
  input.focus(); input.select();
  let finished = false;
  async function commit() {
    if (finished) return; finished = true;
    const newName = input.value.trim();
    if (newName && newName !== oldName) {
      try {
        await fetchJSON('/api/vault/rename', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: filePath, newName }),
        });
      } catch (e) { alert('重命名失败: ' + e.message); }
    }
    await loadTree();
  }
  function cancel() { if (finished) return; finished = true; loadTree(); }
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') commit();
    if (e.key === 'Escape') cancel();
  });
  input.addEventListener('blur', commit);
}

// 删除（软删到 .trash 回收站）：确认后执行
async function deleteNode(path) {
  const name = path.split('/').pop();
  const ok = confirm(`删除「${name}」？\n\n将移到 .trash 回收站（可恢复），不会永久删除。`);
  if (!ok) return;
  try {
    await fetchJSON('/api/vault/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    await loadTree();
  } catch (e) {
    alert('删除失败: ' + e.message);
  }
}

// 取消文件夹（解散）：内容全部移到上级，删文件夹壳
async function unfolderDir(dirPath) {
  const name = dirPath.split('/').pop();
  const ok = confirm(`取消文件夹「${name}」？\n\n里面所有内容将移到上级目录，文件夹本身删除（内容不丢失）。`);
  if (!ok) return;
  try {
    const res = await fetchJSON('/api/vault/unfolder', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: dirPath }),
    });
    await loadTree();
  } catch (e) {
    alert('取消文件夹失败: ' + e.message);
  }
}

// ================= 回收站视图 =================
let trashItems = [];
let trashSelected = new Set();

async function loadTrash() {
  try {
    trashItems = await fetchJSON('/api/vault/trash');
  } catch (e) {
    trashItems = [];
  }
  trashSelected.clear();
  renderTrash();
}

function renderTrash() {
  const tbody = document.getElementById('trashRows');
  const tip = document.getElementById('trashEmptyTip');
  const count = document.getElementById('trashCount');
  count.textContent = trashItems.length ? `（${trashItems.length} 项）` : '';
  tip.style.display = trashItems.length ? 'none' : 'block';
  tbody.innerHTML = trashItems.map(it => `
    <tr>
      <td><input type="checkbox" data-path="${esc(it.path)}" ${trashSelected.has(it.path) ? 'checked' : ''}></td>
      <td>${esc(it.name)}</td>
      <td style="color:var(--dim);font-size:12px">${esc(it.origPath)}</td>
      <td style="color:var(--dim)">${(it.size / 1024).toFixed(1)} KB</td>
      <td style="color:var(--dim)">${it.modTime}</td>
      <td>
        <button class="mini" onclick="trashRestoreOne('${esc(it.path)}')">↩ 恢复</button>
        <button class="mini danger" onclick="trashPurgeOne('${esc(it.path)}')">删除</button>
      </td>
    </tr>`).join('');
}

// 全选/全不选
function trashSelectAll() {
  const cbs = document.querySelectorAll('#trashRows input[type=checkbox]');
  const allChecked = [...cbs].every(c => c.checked);
  cbs.forEach(c => { c.checked = !allChecked; });
  syncTrashSelected();
}
function syncTrashSelected() {
  trashSelected.clear();
  document.querySelectorAll('#trashRows input[type=checkbox]:checked').forEach(c => trashSelected.add(c.dataset.path));
}
document.addEventListener('change', e => {
  if (e.target.matches('#trashRows input[type=checkbox]')) syncTrashSelected();
});

async function trashRestoreOne(path) {
  try {
    const res = await fetchJSON('/api/vault/trash/restore', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    await loadTrash(); await loadTree();
  } catch (e) { alert('恢复失败: ' + e.message); }
}
async function trashPurgeOne(path) {
  if (!confirm('永久删除该文件？此操作不可恢复！')) return;
  try {
    await fetchJSON('/api/vault/trash/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    await loadTrash();
  } catch (e) { alert('删除失败: ' + e.message); }
}
async function trashRestoreSelected() {
  const sel = [...trashSelected];
  if (!sel.length) return alert('请先选择要恢复的文件');
  let ok = 0;
  for (const p of sel) {
    try {
      await fetchJSON('/api/vault/trash/restore', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: p }),
      });
      ok++;
    } catch (e) { /* 跳过失败 */ }
  }
  alert(`已恢复 ${ok}/${sel.length} 项`);
  await loadTrash(); await loadTree();
}
async function trashPurgeSelected() {
  const sel = [...trashSelected];
  if (!sel.length) return alert('请先选择要删除的文件');
  if (!confirm(`永久删除选中的 ${sel.length} 项？此操作不可恢复！`)) return;
  for (const p of sel) {
    try {
      await fetchJSON('/api/vault/trash/delete', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: p }),
      });
    } catch (e) { /* 跳过 */ }
  }
  await loadTrash();
}
async function trashEmpty() {
  if (!trashItems.length) return;
  if (!confirm(`清空回收站（${trashItems.length} 项）？此操作不可恢复！`)) return;
  try {
    await fetchJSON('/api/vault/trash/empty', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
    });
    await loadTrash();
  } catch (e) { alert('清空失败: ' + e.message); }
}
window.trashSelectAll = trashSelectAll;
window.trashRestoreOne = trashRestoreOne;
window.trashPurgeOne = trashPurgeOne;
window.trashRestoreSelected = trashRestoreSelected;
window.trashPurgeSelected = trashPurgeSelected;
window.trashEmpty = trashEmpty;

