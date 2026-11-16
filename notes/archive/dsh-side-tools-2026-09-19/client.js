/// <reference lib="dom" />
/**
 * 浏览器半侧：在「设置 → 插件 → 插件配置」里给本插件渲染一张**权限开关卡片**。
 *
 * 机制（对齐 DSH 的 settings surface）：
 *   · 卡片注册进 `settings.plugin.item` 槽，`key` 必须是宿主侧注册的 settings 命名空间；
 *   · 只有宿主真在服务这个命名空间时，卡片才会被派发渲染；
 *   · 值读写走 `ctx.settingsScope.bind({ namespace })`。
 *
 * 这个文件是**手写的客户端 bundle**（DSH 的客户端模块格式：window.__ModuleLoader__.load 的
 * lazy-CJS factory）。不引 JSX 运行时，只用 React.createElement，便于在没有构建链的情况下维护。
 */
window.__ModuleLoader__.load({
  id: 'dsh-second-brain',
  factory: (require) => {
    const module = { exports: {} };
    const exports = module.exports;
    const React = require('react');
    const h = React.createElement;

    const NS = 'second-brain-permissions';
    const LEVELS = [
      ['read', '只读', '检索、读正文、看块树 / 反向链接 / 事件：无副作用'],
      ['write', '可写', '在只读之上：新建 / 改正文 / 改名 / 移动 / 标签 / 转换导出（产物是新文件，原件不动）'],
      ['trust', '相信', '在可写之上：进出检索库、重建索引、改可见性、删除、加锁解锁、设库密码'],
    ];

    const s = {
      card: { border: '1px solid var(--dsh-border, #333)', borderRadius: 8, padding: '10px 12px' },
      row: { display: 'flex', alignItems: 'flex-start', gap: 8, padding: '4px 0' },
      name: { minWidth: 64, fontWeight: 600 },
      desc: { opacity: 0.72, fontSize: 12, lineHeight: 1.6 },
      bar: { display: 'flex', gap: 8, marginTop: 8 },
      btn: { padding: '2px 10px' },
      hint: { opacity: 0.65, fontSize: 12, marginTop: 6 },
    };

    /** 权限卡片：4 个档位开关 + 保存 / 重置 */
    function PermissionsCard(props) {
      const value = (props && props.value) || {};
      const current = typeof value.level === 'string' ? value.level : 'write';
      const [draft, setDraft] = React.useState(current);
      React.useEffect(() => { setDraft(current); }, [current]);
      const dirty = draft !== current;
      const act = (props && props.actions) || {};
      return h('div', { style: s.card },
        h('div', { style: { fontWeight: 600, marginBottom: 6 } }, '枢阁 · 软件操作权限'),
        h('div', { style: s.desc }, '放到哪一档，就把那一档（含更低的档）的软件操作交给 agent；'
          + '库页面上的按钮不受影响。改完点保存，立刻生效（不用重启 DSH）。'),
        ...LEVELS.map(([id, label, desc]) => h('label', { key: id, style: s.row },
          h('input', {
            type: 'radio', name: 'second-brain-level', checked: draft === id, disabled: !act.set,
            onChange: () => setDraft(id),
          }),
          h('span', { style: s.name }, label),
          h('span', { style: s.desc }, desc))),
        h('div', { style: s.bar },
          h('button', {
            style: s.btn, disabled: !dirty || !act.set,
            onClick: () => act.set({ level: draft }),
          }, '保存'),
          h('button', {
            style: s.btn, disabled: !act.reset,
            onClick: () => act.reset(),
          }, '重置为默认'),
          h('span', { style: s.hint }, dirty ? '有未保存修改' : '')),
        h('div', { style: s.hint },
          '也可以在会话里敲 /brain perm read|write|trust 切换，/brain 看全表。'));
    }

    const inject = ['slots', 'settingsScope'];

    function apply(ctx) {
      const scope = ctx.settingsScope.bind({ namespace: NS });
      const read = () => {
        try {
          const snap = scope.getSnapshot();
          return (snap && (snap.value || snap)) || {};
        } catch (e) {
          return {};
        }
      };
      const actions = {
        /** 逐字段写（DSH 的 settings scope 是 field 级写入；update 只作兜底） */
        set(patch) {
          const keys = Object.keys(patch || {});
          if (typeof scope.set === 'function' && keys.length === 1) {
            try {
              return scope.set(keys[0], patch[keys[0]]);
            } catch (e) {
              /* 落到兜底 */
            }
          }
          try {
            if (typeof scope.set === 'function') {
              let last;
              for (const k of keys) last = scope.set(k, patch[k]);
              return last;
            }
            return scope.update?.(patch);
          } catch (e) {
            return undefined;
          }
        },
        reset() {
          try {
            if (typeof scope.replace === 'function') return scope.replace({});
            return scope.update?.({ level: 'write', deny: [], allow: [] });
          } catch (e) {
            return undefined;
          }
        },
      };
      ctx.slots.inject('settings.plugin.item', () => ctx.slots.register({
        name: 'settings.plugin.item',
        id: NS,
        key: NS,
        order: 40,
        inject: () => ({ value: read(), actions }),
      }, PermissionsCard));
    }

    exports.NS = NS;
    exports.LEVELS = LEVELS;
    exports.PermissionsCard = PermissionsCard;
    exports.apply = apply;
    exports.inject = inject;
    return module.exports;
  },
});
