/**
 * 枢阁工具集 —— agent 接口插件的一部分（不另起插件）。
 *
 * 两条规矩（2026-09-18 辞玖定）：
 *   ① 工具**按危险程度分档**：read / write / risky / danger；
 *   ② **按权限释放**：只有放开的那一档才注册给模型；单个工具还能单独拉黑/放行。
 *
 * 权限开关也在控制台里管：`/brain` 命令（看表 / 开档 / 关档 / 拉黑 / 放行），
 * 改完立刻生效（热同步：新放开的注册、收回的注销），不用重启 DSH。
 *
 * 数据面走库的 HTTP 接口（默认 127.0.0.1:8848，只监听本机）。
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import z from '@deepseek-ai/schemastery';

const HERE = dirname(fileURLToPath(import.meta.url));
// 配置文件位置（测试可用 BRAIN_CONFIG_PATH 指向临时文件，别动真配置）
const CONFIG_PATH = process.env.BRAIN_CONFIG_PATH || join(HERE, '..', 'config.json');

// ── 档位（三档，累积：放到哪一档，就释放到哪一档）────────────────
// 辞玖 2026-09-19 定：只读 / 可写 / 相信。
//   · 只读：只放只读操作
//   · 可写：只读 + 写入（新建/改/改名/移动/标签/转换……产物是新文件，原件不动）
//   · 相信：全都放（含「影响面大」与「危险」：进出检索库、重建、删除、可见性、加锁、设密码）
export const LEVELS = [
  { id: 'read', label: '只读', rank: 0,
    desc: '检索、读正文、看块树/反向链接/事件：无副作用' },
  { id: 'write', label: '可写', rank: 1,
    desc: '在只读之上加：新建 / 改正文 / 改名 / 移动 / 标签 / 新建文件夹 / 转换导出（产物是新文件，原件不动）' },
  { id: 'trust', label: '相信', rank: 2,
    desc: '在可写之上再加：进出检索库、重建索引、改可见性、删除、加锁解锁、设库密码（不可逆或碰隐私的都在这一档）' },
];
export const LEVEL_IDS = LEVELS.map((l) => l.id);
export const rankOf = (id) => {
  const l = LEVELS.find((x) => x.id === id);
  return l ? l.rank : -1;
};

export const DEFAULT_PERMS = { level: 'write', deny: [], allow: [] };

/** 把各种历史形态的权限配置归一成 {level, deny, allow}（兼容老的布尔四档写法） */
export function normalizePerms(raw) {
  const p = raw || {};
  let level = LEVEL_IDS.includes(p.level) ? p.level : null;
  if (!level) {
    if (p.trust === true || p.risky === true || p.danger === true) level = 'trust';
    else if (p.write === true || p.read === true) level = 'write';
    else level = DEFAULT_PERMS.level;
  }
  return {
    level,
    deny: Array.isArray(p.deny) ? p.deny : [],
    allow: Array.isArray(p.allow) ? p.allow : [],
  };
}

export const DEFAULT_LIB_URL = 'http://127.0.0.1:8848';

// ── 权限的"家"：DSH 的设置文档 ───────────────────────────────
// 放进设置服务（namespace）而不是只藏在 config.json，是为了让它在控制台
// 「设置 → 插件 → 插件配置」里有一张**可点选的卡片**（浏览器半侧 src/client.js）。
// 分层：schema 默认值 < 本插件的部署配置(config.json) = base < 用户层（卡片/命令写这里）。
export const PERM_NS = 'second-brain-permissions';

export const PermissionSchema = z.object({
  level: z.string().default(DEFAULT_PERMS.level),   // read | write | trust（归一校验在 normalizePerms）
  deny: z.array(z.string()).default([]),
  allow: z.array(z.string()).default([]),
});

const state = { scope: null, cfg: null, onChange: null };

/** 当前权限值（设置服务在手就用它的解析值，否则用配置对象） */
export function livePerms() {
  if (state.scope) {
    try {
      return { ...DEFAULT_PERMS, ...(state.scope.get() || {}) };
    } catch {
      /* 落到配置 */
    }
  }
  return permsOf(state.cfg ?? {});
}

/**
 * 把权限挂到设置服务上：注册 namespace（base = config.json 里的值），
 * 读一次当前值，并监听后续变更 → 写回 cfg 并触发热同步。
 * @returns {{ok: boolean, reason?: string, scope?: object}}
 */
export function attachSettings(ctx, cfg, onChange) {
  state.cfg = cfg;
  state.onChange = onChange ?? null;
  if (!ctx?.settings || typeof ctx.settings.register !== 'function') {
    return { ok: false, reason: '设置服务不在（没挂 dsh-settings-file 之类的提供方）' };
  }
  let scope;
  try {
    scope = ctx.settings.register(PERM_NS, PermissionSchema, { base: permsOf(cfg) });
  } catch (e) {
    return { ok: false, reason: `注册权限命名空间失败：${e?.message ?? e}` };
  }
  state.scope = scope;
  const apply = (next) => {
    cfg.tools = { ...(cfg.tools ?? {}), permissions: { ...DEFAULT_PERMS, ...(next || {}) } };
    state.onChange?.();
  };
  apply(typeof scope.get === 'function' ? scope.get() : permsOf(cfg));
  if (typeof scope.watch === 'function') scope.watch((next) => apply(next));
  return { ok: true, scope };
}

/** 重置权限（清空用户层，回到 base = 部署配置） */
export function resetPerms() {
  if (state.scope?.replace) {
    state.scope.replace({});
    return true;
  }
  return false;
}

// ── 工具表 ───────────────────────────────────────────────────
const S = (t, d, extra = {}) => ({ type: t, description: d, ...extra });

/** 每个工具：name / level / desc / parameters / call（怎么打到库的 HTTP 上） */
export const TOOLS = [
  // ── read：只读，无副作用 ────────────────────────────────
  {
    name: 'brain_search', level: 'read', op: 'GET /api/search',
    desc: '在枢阁里检索（关键词 + 语义 + 引用关系融合）。返回命中片段：文件名、路径、小节、位置（第几页）。要整篇再用 brain_read。',
    parameters: {
      type: 'object', required: ['q'], properties: {
        q: S('string', '查询词'),
        limit: S('integer', '返回几条，默认 8'),
        scope: S('string', 'all=连归档日志一起搜（默认排除）'),
        since: S('string', '按笔记日期收窄：起（YYYY-MM-DD）'),
        until: S('string', '按笔记日期收窄：止'),
        mode: S('string', 'kw=只走关键词（快）'),
      },
    },
    call: { method: 'GET', path: '/api/search', query: ['q', 'limit', 'scope', 'since', 'until', 'mode'] },
  },
  {
    name: 'brain_read', level: 'read', op: 'GET /api/content',
    desc: '按库内路径取一篇的正文（可截断）。路径形如 知识库/01-编程通识/08-调试方法论（扩展名可省）。不知道路径先用 brain_search / brain_tree。',
    parameters: {
      type: 'object', required: ['path'], properties: {
        path: S('string', '库内路径'),
        max: S('integer', '最多取多少字符'),
      },
    },
    call: { method: 'GET', path: '/api/content', query: ['path', 'max'] },
  },
  {
    name: 'brain_pack', level: 'read', op: 'POST /api/content/batch',
    desc: '一次取多篇正文（组"注入包"用）：给一串库内路径，回每篇的正文（可截断）。',
    parameters: {
      type: 'object', required: ['paths'], properties: {
        paths: { type: 'array', items: { type: 'string' }, description: '库内路径数组' },
        max: S('integer', '每篇最多多少字'),
      },
    },
    call: { method: 'POST', path: '/api/content/batch', body: ['paths', 'max'] },
  },
  {
    name: 'brain_tree', level: 'read', op: 'GET /api/tree',
    desc: '列出库的文件夹树（含每层文件、kind、是否在检索库里）。找路径时先用它。',
    parameters: { type: 'object', properties: {} },
    call: { method: 'GET', path: '/api/tree' },
  },
  {
    name: 'brain_folders', level: 'read', op: 'GET /api/folders',
    desc: '文件夹扁平表（含路径），给"移动到"用。',
    parameters: { type: 'object', properties: {} },
    call: { method: 'GET', path: '/api/folders' },
  },
  {
    name: 'brain_tags', level: 'read', op: 'GET /api/tags',
    desc: '标签统计（哪些标签、各有多少篇）。',
    parameters: { type: 'object', properties: {} },
    call: { method: 'GET', path: '/api/tags' },
  },
  {
    name: 'brain_note', level: 'read', op: 'GET /api/files/{id}',
    desc: '读一篇的元信息：标签、@引用（我引了谁）、反向链接（谁引了我）、派生摘要、块树统计。',
    parameters: { type: 'object', required: ['id'], properties: { id: S('string', '文件 id') } },
    call: { method: 'GET', path: '/api/files/{id}' },
  },
  {
    name: 'brain_backlinks', level: 'read', op: 'GET /api/files/{id}/backlinks',
    desc: '谁引用了这一篇（可按小节筛）。',
    parameters: {
      type: 'object', required: ['id'], properties: {
        id: S('string'), section: S('string', '只看引这一节的'),
      },
    },
    call: { method: 'GET', path: '/api/files/{id}/backlinks', query: ['section'] },
  },
  {
    name: 'brain_blocks', level: 'read', op: 'GET /api/files/{id}/blocks',
    desc: '一篇文档的块树（页 / 块类型 / 标题层级 / 坐标 / 表格数据）。',
    parameters: {
      type: 'object', required: ['id'], properties: {
        id: S('string'), page: S('integer', '只看某页'),
        type: S('string', 'heading/paragraph/table/header/footer/figure'),
      },
    },
    call: { method: 'GET', path: '/api/files/{id}/blocks', query: ['page', 'type'] },
  },
  {
    name: 'brain_article', level: 'read', op: 'GET /api/files/{id}/article',
    desc: '把一篇按整篇或某一节还原出来（长文阅读 / 组上下文）。',
    parameters: {
      type: 'object', required: ['id'], properties: {
        id: S('string'), focus: S('string', '聚焦到哪一节'), section: S('string'),
        max_chars: S('integer'),
      },
    },
    call: { method: 'GET', path: '/api/files/{id}/article', query: ['focus', 'section', 'max_chars'] },
  },
  {
    name: 'brain_health', level: 'read', op: 'GET /api/health',
    desc: '库的统计：文件数 / 块数 / 向量缺口 / 未入检索数 / 嵌入规格 / schema 版本。',
    parameters: { type: 'object', properties: {} },
    call: { method: 'GET', path: '/api/health' },
  },
  {
    name: 'brain_events', level: 'read', op: 'GET /api/events',
    desc: '增量读库的事件（谁在什么时候建/改/删/加锁…），可用于「最近动了什么」。',
    parameters: {
      type: 'object', properties: {
        after: S('integer'), limit: S('integer'), types: S('string', '如 note.*'),
      },
    },
    call: { method: 'GET', path: '/api/events', query: ['after', 'limit', 'types'] },
  },
  {
    name: 'brain_engines', level: 'read', op: 'GET /api/convert/engines',
    desc: '转换引擎状态：pandoc 版本、WPS 可用否、能写出哪些格式。',
    parameters: { type: 'object', properties: {} },
    call: { method: 'GET', path: '/api/convert/engines' },
  },
  {
    name: 'brain_selfcheck', level: 'read', op: 'GET /api/maintenance/selfcheck',
    desc: '跑一遍库自检（一致性 / 引用图 / 备份 / 卫生），返回逐项结论。',
    parameters: { type: 'object', properties: {} },
    call: { method: 'GET', path: '/api/maintenance/selfcheck', timeout: 300000 },
  },

  // ── write：写入，产物是新文件、原件不动、可撤销 ─────────────
  {
    name: 'brain_create', level: 'write', op: 'POST /api/files',
    desc: '新建一篇笔记（Markdown）。名字写 .docx/.odt/.pdf 会现场生成真文档（这种默认不进检索库）。',
    parameters: {
      type: 'object', required: ['name', 'content'], properties: {
        name: S('string', '含扩展名，如 研讨会记录.md'),
        content: S('string', '正文（Markdown）'),
        folder_id: S('string', '放哪个文件夹，默认根目录'),
      },
    },
    call: { method: 'POST', path: '/api/files', body: ['name', 'content', 'folder_id'] },
  },
  {
    name: 'brain_update', level: 'write', op: 'PUT /api/files/{id}',
    desc: '保存一篇的正文（会重新切块、重建 @引用、补向量）。只有文本格式能改；文档格式先 brain_convert 转 Markdown。',
    parameters: {
      type: 'object', required: ['id', 'content'],
      properties: { id: S('string'), content: S('string', '新的完整正文') },
    },
    call: { method: 'PUT', path: '/api/files/{id}', body: ['content'] },
  },
  {
    name: 'brain_rename', level: 'write', op: 'POST /api/files/{id}/rename',
    desc: '改名。别人正文里对它的引用会跟着改；指向它小节的引用也会跟随。',
    parameters: {
      type: 'object', required: ['id', 'name'],
      properties: { id: S('string'), name: S('string', '新名字（含扩展名）') },
    },
    call: { method: 'POST', path: '/api/files/{id}/rename', body: ['name'] },
  },
  {
    name: 'brain_move', level: 'write', op: 'POST /api/files/{id}/move',
    desc: '把一篇移到别的文件夹（按库内路径，路径不存在会建）。',
    parameters: {
      type: 'object', required: ['id', 'path'],
      properties: { id: S('string'), path: S('string', '目标库内路径') },
    },
    call: { method: 'POST', path: '/api/files/{id}/move', body: ['path'] },
  },
  {
    name: 'brain_tag', level: 'write', op: 'POST /api/files/{id}/tags',
    desc: '给一篇加/去/设标签（标签也用于检索排除，如 logs 归档）。',
    parameters: {
      type: 'object', required: ['id', 'tags'],
      properties: {
        id: S('string'), tags: { type: 'array', items: { type: 'string' } },
        mode: S('string', 'add（默认）/ remove / set'),
      },
    },
    call: { method: 'POST', path: '/api/files/{id}/tags', body: ['tags', 'mode'] },
  },
  {
    name: 'brain_new_folder', level: 'write', op: 'POST /api/folders',
    desc: '新建文件夹（给 parent_id 或库内路径 path）。',
    parameters: {
      type: 'object', required: ['name'], properties: {
        name: S('string', '文件夹名'),
        parent_id: S('string', '父文件夹 id'),
        path: S('string', '父路径（与 parent_id 二选一）'),
      },
    },
    call: { method: 'POST', path: '/api/folders', body: ['name', 'parent_id', 'path'] },
  },
  {
    name: 'brain_rename_folder', level: 'write', op: 'POST /api/folders/{id}/rename',
    desc: '文件夹改名（引用处的路径会跟着改）。',
    parameters: {
      type: 'object', required: ['id', 'name'],
      properties: { id: S('string'), name: S('string') },
    },
    call: { method: 'POST', path: '/api/folders/{id}/rename', body: ['name'] },
  },
  {
    name: 'brain_convert', level: 'write', op: 'POST /api/files/{id}/convert',
    desc: '格式转换 / 导出：文档 → Markdown（to=md），或当前内容 → docx/pdf/odt/rtf/html/epub。产物是同目录新文件，原件不动。',
    parameters: {
      type: 'object', required: ['id', 'to'],
      properties: {
        id: S('string'), to: S('string', 'md|docx|odt|rtf|html|epub|pdf'),
        name: S('string', '产物文件名（默认同名换扩展名）'),
      },
    },
    call: { method: 'POST', path: '/api/files/{id}/convert', body: ['to', 'name'], timeout: 300000 },
  },

  // ── trust：影响面大 / 不可逆（severity 只备查，闸门就是「相信」这一档）──
  {
    name: 'brain_rag', level: 'trust', severity: 'risky', op: 'POST /api/files/{id}/rag',
    desc: '把一篇加入或移出检索库。移出后文件还在，但搜不到（块与向量会被清掉）。',
    parameters: {
      type: 'object', required: ['id', 'on'],
      properties: { id: S('string'), on: S('boolean', 'true=加入检索库，false=移出') },
    },
    call: { method: 'POST', path: '/api/files/{id}/rag', body: ['on'] },
  },
  {
    name: 'brain_reindex', level: 'trust', severity: 'risky', op: 'POST /api/reindex',
    desc: '重建索引。默认只重建变过的（秒级）；with_vectors 顺带补向量缺口；full=true 才全量（慢）。',
    parameters: {
      type: 'object', properties: {
        reason: S('string'), with_vectors: S('boolean'), full: S('boolean'),
      },
    },
    call: { method: 'POST', path: '/api/reindex', body: ['reason', 'with_vectors', 'full'], timeout: 600000 },
  },
  {
    name: 'brain_relink_section', level: 'trust', severity: 'risky', op: 'POST /api/files/{id}/sections/relink',
    desc: '把「指向某小节」的引用统一改成新小节名（小节改名后修引用用）。',
    parameters: {
      type: 'object', required: ['id', 'old', 'new'],
      properties: { id: S('string'), old: S('string'), new: S('string') },
    },
    call: { method: 'POST', path: '/api/files/{id}/sections/relink', body: ['old', 'new'] },
  },
  {
    name: 'brain_replay_events', level: 'trust', severity: 'risky', op: 'GET /api/events/replay',
    desc: '按事件序号区间重放（dry_run=1 只预演不写）。',
    parameters: {
      type: 'object', properties: {
        from: S('integer'), to: S('integer'), types: S('string'), dry_run: S('string', '1=只预演'),
      },
    },
    call: { method: 'GET', path: '/api/events/replay', query: ['from', 'to', 'types', 'dry_run'],
            timeout: 600000 },
  },

  {
    name: 'brain_delete', level: 'trust', severity: 'danger', op: 'DELETE /api/files/{id}',
    desc: '删除一篇（立墓碑，引用它的地方会显示「已删除」）。不可逆，必须先说明影响范围再执行。',
    parameters: {
      type: 'object', required: ['id'],
      properties: { id: S('string'), confirm: S('boolean', '确认删除') },
    },
    call: { method: 'DELETE', path: '/api/files/{id}' },
  },
  {
    name: 'brain_drop_folder', level: 'trust', severity: 'danger', op: 'DELETE /api/folders/{id}',
    desc: '删空文件夹（里面有东西就删不掉）。',
    parameters: {
      type: 'object', required: ['id'],
      properties: { id: S('string'), confirm: S('boolean') },
    },
    call: { method: 'DELETE', path: '/api/folders/{id}' },
  },
  {
    name: 'brain_visibility', level: 'trust', severity: 'danger', op: 'POST /api/files/{id}/visibility',
    desc: '改可见性：public / internal / private（private 上锁后不进检索）。',
    parameters: {
      type: 'object', required: ['id', 'visibility'],
      properties: { id: S('string'), visibility: S('string'), confirm: S('boolean') },
    },
    call: { method: 'POST', path: '/api/files/{id}/visibility', body: ['visibility'] },
  },
  {
    name: 'brain_lock', level: 'trust', severity: 'danger', op: 'POST /api/lock/lock',
    desc: '锁库 / 解锁（私密内容随锁从检索里消失；解锁默认 15 分钟）。',
    parameters: {
      type: 'object', required: ['action'],
      properties: {
        action: S('string', 'lock=立刻上锁，unlock=解锁'),
        password: S('string', 'action=unlock 时必填'),
      },
    },
    call: { method: 'POST', path: '/api/lock/{action}', body: ['password'] },
  },
  {
    name: 'brain_set_password', level: 'trust', severity: 'danger', op: 'POST /api/lock/set',
    desc: '首次给库设密码（之后私密内容靠它保护）。',
    parameters: {
      type: 'object', required: ['password', 'confirm'],
      properties: { password: S('string'), confirm: S('string', '再输一次') },
    },
    call: { method: 'POST', path: '/api/lock/set', body: ['password', 'confirm'] },
  },
];

export const BY_NAME = Object.fromEntries(TOOLS.map((t) => [t.name, t]));

// ── 权限：读 / 判 / 写 ───────────────────────────────────────
export function permsOf(cfg) {
  void z;   // 保持 schemastery 依赖在模块顶部（PermissionSchema 用）
  return normalizePerms(cfg?.tools?.permissions ?? cfg?.permissions);
}

/** 某入口现在能不能给模型用（累积式：放到哪一档就释放到哪一档）；返回 {ok, why} */
export function allowed(cfg, tool) {
  const p = permsOf(cfg);
  if (p.deny.includes(tool.name)) return { ok: false, why: '被单独拉黑' };
  if (p.allow.includes(tool.name)) return { ok: true, why: '被单独放行' };
  return rankOf(tool.level) <= rankOf(p.level)
    ? { ok: true, why: `档已放开（${p.level} ≥ ${tool.level}）` }
    : { ok: false, why: `档没放到（需要 ${tool.level}）` };
}

export function released(cfg) {
  return TOOLS.filter((t) => allowed(cfg, t).ok);
}

/** 权限表（给人和模型看） */
export function permissionTable(cfg) {
  const p = permsOf(cfg);
  const lines = [`软件操作权限（库 ${libraryUrl(cfg)}）`,
    `存放：${state.scope ? `控制台设置文档 · 命名空间 ${PERM_NS}` : 'config.json（设置服务不在）'}`,
    `现在放到：${p.level}（${(LEVELS.find((l) => l.id === p.level) || {}).label || '?'}）`
    + `　拉黑：${p.deny.length ? p.deny.join(',') : '无'}　单独放行：${p.allow.length ? p.allow.join(',') : '无'}`];
  for (const lv of LEVELS) {
    lines.push(`── ${lv.label}（${lv.id}，${lv.desc}）`);
    for (const t of TOOLS.filter((x) => x.level === lv.id)) {
      const a = allowed(cfg, t);
      lines.push(`   ${a.ok ? '✅释放' : '⛔收起'} ${t.name.padEnd(22)} ${(t.op || '').padEnd(34)} ${a.why}`);
    }
  }
  lines.push(`给模型的工具：${released(cfg).length} / ${TOOLS.length}`);
  return lines.join('\n');
}

/** 改权限并写回 config.json（返回一句反馈） */
export function changePerm(cfg, argv) {
  const p = permsOf(cfg);
  const [a, b] = argv;
  if (!a) return { changed: false, text: permissionTable(cfg) };
  if (LEVEL_IDS.includes(a)) {
    p.level = a;                                   // 三档：放到哪一档就释放到哪一档
  } else if (a === 'deny' || a === 'allow') {
    if (!b || !BY_NAME[b]) return { changed: false, text: `不认识这个工具：${b ?? '(空)'}` };
    if (a === 'deny') {
      p.deny = [...new Set([...p.deny, b])];
      p.allow = p.allow.filter((x) => x !== b);
    } else {
      p.allow = [...new Set([...p.allow, b])];
      p.deny = p.deny.filter((x) => x !== b);
    }
  } else if (a === 'reset') {
    p.level = DEFAULT_PERMS.level;
    p.deny = [];
    p.allow = [];
  } else {
    return { changed: false,
             text: '用法：/brain perm read|write|trust ｜ /brain perm deny|allow <入口> ｜ /brain perm reset' };
  }
  if (state.scope && typeof state.scope.update === 'function') {
    // 首选：写进设置文档的用户层（持久化 + 广播 + 版本检查，控制台卡片读同一份）
    Promise.resolve(state.scope.update(p)).catch(() => {});
    cfg.tools = { ...(cfg.tools ?? {}), permissions: p };
    state.onChange?.();
  } else {
    // 兜底：没有设置服务时写 config.json（老行为）
    const rawPermissions = (() => {
      try {
        return JSON.parse(readFileSync(CONFIG_PATH, 'utf8'));
      } catch {
        return {};
      }
    })();
    const merged = { ...rawPermissions };
    merged.tools = { ...(merged.tools ?? {}), ...(cfg?.tools ?? {}), permissions: p };
    try {
      writeFileSync(CONFIG_PATH, `${JSON.stringify(merged, null, 2)}\n`, 'utf8');
    } catch (e) {
      return { changed: false, text: `权限没写进 config.json：${e?.message ?? e}` };
    }
    cfg.tools = merged.tools;
  }
  return { changed: true, text: `已改：放到 ${p.level} 档`
    + `　拉黑：${p.deny.join(',') || '无'}　单独放行：${p.allow.join(',') || '无'}` };
}

// ── 打库的 HTTP ──────────────────────────────────────────────
export function libraryUrl(cfg) {
  return String(cfg?.libraryUrl || cfg?.tools?.libraryUrl || DEFAULT_LIB_URL).replace(/\/+$/, '');
}

export async function callLibrary(cfg, method, path, query, body, timeout = 120000) {
  let url = libraryUrl(cfg) + path;
  const clean = Object.fromEntries(Object.entries(query ?? {})
    .filter(([, v]) => v !== undefined && v !== null && v !== ''));
  if (Object.keys(clean).length) url += `?${new URLSearchParams(clean)}`;
  const init = { method, signal: AbortSignal.timeout(timeout) };
  if (body !== undefined) {
    init.headers = { 'content-type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, init);
  } catch (e) {
    throw new Error(`连不上库（${libraryUrl(cfg)}）：${e?.message ?? e}`);
  }
  let json = null;
  try {
    json = await res.json();
  } catch {
    /* 非 JSON */
  }
  if (!res.ok || (json && json.ok === false)) {
    throw new Error((json && json.error) || `${method} ${path} → HTTP ${res.status}`);
  }
  // 库的约定：没找到也回 200，但 item.found = false → 对模型要变成明确错误，
  // 否则模型会拿到一个空壳当成"读到了"。 （如 /api/content?path=不存在）
  if (json && json.item && json.item.found === false) {
    throw new Error(String(json.item.error || 'not found'));
  }
  return json ?? { ok: true };
}

// ── 注册（可热同步：收回的注销、新放开的注册）─────────────────
const contentOutput = {
  schema: {
    type: 'object',
    properties: { content: { type: 'array', items: {} } },
    required: ['content'],
    additionalProperties: false,
  },
  render(_args, value) {
    return Array.isArray(value?.content) ? value.content : [{ type: 'text', text: String(value ?? '') }];
  },
};

/**
 * 把放开的工具注册到 DSH；返回 sync()，权限一变调它一次即可热更。
 * @param {object} ctx cordis 上下文（需要 ctx.tools）
 * @param {object} cfg 插件配置（config.json 的对象）
 */
export function registerLibraryTools(ctx, cfg) {
  const live = new Map(); // name -> dispose
  const limit = Number(cfg?.tools?.maxResultChars ?? cfg?.maxResultChars ?? 120000);

  function sync() {
    const want = new Map(released(cfg).map((t) => [t.name, t]));
    for (const [name, dispose] of [...live]) {
      if (!want.has(name)) {
        try {
          dispose?.();
        } catch {
          /* 注销失败不致命 */
        }
        live.delete(name);
      }
    }
    for (const [name, tool] of want) {
      if (live.has(name)) continue;
      const definition = {
        name,
        description: `${tool.desc}（枢阁 · ${tool.level} 档）`,
        parameters: tool.parameters,
        output: contentOutput,
        async execute(args = {}) {
          const c = tool.call;
          let path = c.path;
          for (const [k, v] of Object.entries(args)) {
            if (path.includes(`{${k}}`)) path = path.replaceAll(`{${k}}`, encodeURIComponent(String(v)));
          }
          if (path.includes('{')) throw new Error(`缺少参数：${path.match(/\{(\w+)\}/)?.[1] ?? '?'}`);
          const query = Object.fromEntries((c.query ?? []).map((k) => [k, args[k]]));
          const body = c.body === undefined
            ? undefined
            : Object.fromEntries(c.body.filter((k) => args[k] !== undefined).map((k) => [k, args[k]]));
          const r = await callLibrary(cfg, c.method, path, query, body, c.timeout);
          let text = JSON.stringify(r);
          if (text.length > limit) text = `${text.slice(0, limit)}…（截断，用更小的 max/limit 再取）`;
          return { content: [{ type: 'text', text }] };
        },
      };
      try {
        live.set(name, ctx.tools.register(definition));
      } catch (e) {
        console.warn('[second-brain] 工具注册失败', name, e?.message ?? e);
      }
    }
  }

  sync();
  return { sync, count: () => live.size };
}

/**
 * `/brain` 命令：在控制台里看 / 改权限（命令列表里就能看到，不需要额外前端）。
 * 子命令：perm [档 on|off] ｜ perm deny|allow <工具> ｜ perm reset ｜ tools ｜ status
 */
export function registerBrainCommand(ctx, cfg, hooks = {}) {
  const handler = async (invocation) => {
    const raw = String(invocation?.input?.text ?? invocation?.text ?? invocation?.args ?? '').trim();
    const argv = raw.split(/\s+/).filter(Boolean);
    const [sub, ...rest] = argv;
    try {
      if (!sub || sub === 'perm' || sub === 'perms') {
        if (sub === 'perm' && rest.length) {
          const r = changePerm(cfg, rest);
          hooks.onPermChange?.();
          return { kind: r.changed ? 'text' : 'error', text: `${r.text}\n\n${permissionTable(cfg)}` };
        }
        return { kind: 'text', text: permissionTable(cfg) };
      }
      if (sub === 'tools') {
        return {
          kind: 'text',
          text: released(cfg).map((t) => `${t.name.padEnd(22)} ${t.level.padEnd(7)} ${t.desc}`).join('\n'),
        };
      }
      if (sub === 'status') {
        const h = await callLibrary(cfg, 'GET', '/api/health', {}, undefined, 15000);
        const s = h?.stats ?? {};
        return {
          kind: 'text',
          text: `库：文件 ${s.files} / 块 ${s.chunks} / 块树 ${s.blocks} / 未入检索 ${s.rag_off}\n`
            + `待补向量 ${h?.pending_vectors} ｜ 嵌入规格 ${h?.manifest?.embed_spec} ｜ schema ${h?.manifest?.schema_version}\n`
            + `工具：${released(cfg).length}/${TOOLS.length} 已释放`,
        };
      }
      return { kind: 'error', text: '用法：/brain [perm [档 on|off] | perm deny|allow <工具> | perm reset | tools | status]' };
    } catch (e) {
      return { kind: 'error', text: `枢阁：${e?.message ?? e}` };
    }
  };
  return ctx.commands.register({
    name: 'brain',
    description: '枢阁：看/改工具权限、看库状态',
    input: { hint: '[perm [read|write|risky|danger on|off] | perm deny|allow <工具> | tools | status]' },
    handler,
  });
}
