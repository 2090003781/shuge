//go:build windows

// 本文件：枢阁主进程的页面（内嵌 HTML，不依赖任何外部资源/CDN）= **控制台**。
//
// 定案（辞玖 2026-09-20）：**控制台 = 主进程页**（常驻、内核不在也照样打得开），
// 页面按"主体 + 两个下属"分三层，外加日志与自检：
//
//	① 整体生命   主进程自己：打开（桌面图标）/ 关闭（本页按钮）；顺带一行下属状态
//	② 📜 日志    并入控制台：来源切换 + 聚合时间线（内核不在也能看，因为直接读 data/logs/*.log）
//	③ 🩺 自检    平台体检（只在跃迁时留痕）
//
// 库与内核不再各占一张卡片（辞玖 2026-09-20："内核和库的卡片不需要再放在控制台"）：
// 它们就是顶栏的两个标签页（库 = /notes/、内核 = /core/），内核的启停按钮放在内核页顶部那条小面板里。
//
// 生命周期语义（辞玖 2026-09-20）：库与内核**默认开启 + 自动重连**；
// 「自动重连」关掉 = 挂了不再自动拉起（不杀正在跑的），再打开就立刻确保在跑。
//
// 一句话：**用户打开的是笔记软件** —— 内核不在、库不在，控制台都得在。
package main

const hostPageHTML = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>枢阁 · 控制台</title>
<style>
:root { --bg:#16161e; --panel:#1a1b26; --panel2:#1f2335; --panel3:#24283b; --bar:#20233a; --line:#3b4261; --line2:#2f3549; --fg:#c0caf5; --fg2:#9aa5ce; --dim:#565f89; --blue:#7aa2f7; --green:#9ece6a; --red:#f7768e; --amber:#e0af68; --purple:#bb9af7; --sel:#2d3350; --hover:#232433; --hit:#20222f; --edge:#101018;  --bg2:#1c1d2b; --bg3:#2a2e42; --sel2:#22304d; --sel3:#3d4670; --info-bg:#2a3a5c; --ok-bg:#1f3d2b; --ok-bg2:#233024; --warn-bg:#4d3a1f; --warn-bg2:#4a3b1f; --warn-bg3:#2a271d; --bad-bg:#3a2a30; --ok-bg3:#3d4a2a; --ok-bg4:#4b6a2f; --dim2:#8b93b8; --fg3:#a9b1d6; --cyan:#7dcfff; --blue2:#89b4fa; --pink:#f7a8b8; --red2:#d99; --magenta:#e8a; --amber2:#ffd479; --brown:#6b4a2f; --brown2:#6b5a2f; --green2:#2f6b43; --ok-fg:#e6ffd8; --primary:#2b3f66; --primary-line:#3d5a8a; --hover2:#262a45; --warn-fg2:#e0c07a; --warn-bg4:#3a3320; --line3:#2a2f45; --bg4:#15161f; --bg5:#1f2233; --on-primary:#c0caf5;}

  *{box-sizing:border-box}
  body{margin:0;background:var(--panel);color:var(--fg);font:14px/1.6 ui-sans-serif,"Microsoft YaHei",system-ui,sans-serif}
  .wrap{max-width:1100px;margin:0 auto;padding:16px}
  .hd{display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap}
  .brand{font-size:19px;font-weight:bold;color:var(--fg)}
  .pill{padding:2px 10px;border-radius:999px;font-size:12px;background:var(--panel3);color:var(--fg2);border:1px solid var(--line)}
  .pill.ok{color:var(--green);border-color:var(--green)55}
  .pill.bad{color:var(--red);border-color:var(--red)55}
  .pill.blue{color:var(--blue);border-color:var(--blue)55;text-decoration:none}
  .card{background:var(--panel2);border:1px solid var(--line2);border-radius:10px;padding:14px 16px;margin-bottom:12px}
  .card.warn{background:var(--warn-bg3);border-color:var(--amber)55;color:var(--amber)}
  .hd2{font-size:15px;color:var(--blue);font-weight:bold;margin-bottom:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .row1{display:flex;align-items:center;gap:10px}
  .dot{width:10px;height:10px;border-radius:50%;background:var(--dim);flex:0 0 auto}
  .dot.ok{background:var(--green)}.dot.warn{background:var(--amber)}.dot.bad{background:var(--red)}
  .state{font-size:18px;font-weight:bold}
  .meta{color:var(--dim2);font-size:12.5px;margin-top:6px;line-height:1.9}
  .kids{display:flex;gap:18px;flex-wrap:wrap;margin-top:10px;font-size:13px}
  .kids .k{background:var(--bg);border:1px solid var(--line2);border-radius:8px;padding:6px 12px}
  .btns{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap;align-items:center}
  button.big{font-size:14px;padding:9px 18px;border-radius:8px;border:none;background:var(--blue);color:var(--panel);font-weight:bold;cursor:pointer}
  a.big{font-size:14px;padding:9px 18px;border-radius:8px;background:var(--blue);color:var(--panel);font-weight:bold;cursor:pointer;display:inline-block}
  button.big.ghost,a.big.ghost{background:var(--panel3);color:var(--fg2);border:1px solid var(--line);font-weight:normal}
  button.big.stop{background:var(--red);color:var(--panel)}
  button.big:hover,a.big:hover{filter:brightness(1.1)}
  button.mini{font-size:12px;padding:3px 10px;border-radius:6px;border:1px solid var(--line);background:var(--panel3);color:var(--fg2);cursor:pointer}
  button.mini:hover{background:var(--line2)}
  .note{color:var(--dim);font-size:12.5px;margin-top:8px;line-height:1.8}
  .role{color:var(--fg2);font-size:12.5px;margin-top:6px;border-left:2px solid var(--line);padding-left:8px}
  .patrol{display:flex;align-items:center;gap:8px;margin-bottom:12px;padding:8px 12px;border-radius:8px;background:var(--panel2);border:1px solid var(--line);font-size:12.5px;color:var(--fg2);flex-wrap:wrap}
  .patrol.ok{border-color:rgba(158,206,106,.45)}
  .patrol.warn{border-color:rgba(224,175,104,.5);background:var(--warn-bg3)}
  .patrol.fail{border-color:rgba(247,118,142,.55);background:var(--bad-bg);color:var(--pink)}
  .patrol .t{color:var(--dim)}
  .tag{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--fg2)}
  .tag.ok{color:var(--green);border-color:var(--green)55}
  .tag.warn{color:var(--amber);border-color:var(--amber)55}
  .tag.bad{color:var(--red);border-color:var(--red)55}
  .checklist{font-size:13px;line-height:1.6}
  .ck{display:flex;flex-wrap:wrap;gap:8px;padding:5px 0;border-bottom:1px solid var(--bg3)}
  .ck:last-child{border-bottom:0}
  .ck .ic{flex:0 0 auto}
  .ck .nm{flex:0 0 160px;color:var(--fg)}
  .ck .dt{flex:1;color:var(--dim2);word-break:break-word}
  .ck .ht{flex:0 0 100%;color:var(--amber);font-size:12px;padding-left:22px}
  .ck.warn .nm{color:var(--amber)}
  .ck.fail .nm{color:var(--red)}
  .ck.skip .nm{color:var(--dim)}
  .ck.known .nm{color:var(--cyan)}
  /* 日志区（原顶栏「日志」页并入控制台） */
  .log-wrap{display:flex;height:380px;border:1px solid var(--line2);border-radius:8px;overflow:hidden}
  .log-side{width:210px;flex-shrink:0;background:var(--panel);border-right:1px solid var(--line2);overflow-y:auto;padding:6px}
  .log-src{padding:6px 8px;border-radius:6px;cursor:pointer;margin-bottom:2px}
  .log-src:hover{background:var(--panel3)}
  .log-src.active{background:var(--line)}
  .log-src .nm{font-size:13px;color:var(--fg3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .log-src.active .nm{color:var(--blue)}
  .log-src .meta{font-size:11px;color:var(--dim);margin-top:2px}
  .log-main{flex:1;display:flex;flex-direction:column;overflow:hidden}
  .log-bar{display:flex;gap:10px;align-items:center;padding:6px 10px;background:var(--bg);border-bottom:1px solid var(--line2);flex-wrap:wrap}
  .log-bar select,.log-bar input{background:var(--panel3);color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:3px 6px;font-size:12px}
  .log-bar input{width:170px}
  .log-bar label{font-size:12px;color:var(--dim);display:flex;align-items:center;gap:4px}
  .log-stream{flex:1;overflow-y:auto;background:var(--bg);padding:6px 0;font-family:ui-monospace,Consolas,monospace;font-size:12px}
  .log-line{display:flex;gap:8px;padding:1px 10px;white-space:pre-wrap;word-break:break-all}
  .log-line:hover{background:var(--panel2)}
  .log-line .t{color:var(--dim);flex-shrink:0}
  .log-line .lv{width:46px;flex-shrink:0;font-weight:bold}
  .log-line .src{color:var(--green);width:160px;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .log-line .tx{flex:1;color:var(--fg3)}
  .foot{font-size:12px;color:var(--dim);line-height:1.9;text-align:center;padding:6px 0 20px}
  /* 顶部标签：控制台 / 库 / 内核 三页同页切换（辞玖 2026-09-20：点一下就切，方便） */
  .tabs{display:flex;align-items:center;gap:6px;padding:8px 14px;background:var(--bg);border-bottom:1px solid var(--line2);position:sticky;top:0;z-index:5;flex-wrap:wrap}
  .tabs .brand{font-size:15px;font-weight:bold;color:var(--fg);margin-right:10px}
  .tab{background:transparent;border:none;color:var(--dim);padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px}
  .tab:hover{color:var(--fg)}
  .tab.active{background:var(--panel3);color:var(--cyan)}
  .tab .td{margin-left:5px;font-size:11px}
  .view{display:none}
  .view.active{display:flex;flex-direction:column;min-height:0}
  /* 面板容器（2026-10-10：取代 iframe）：撑满剩余高度、自己滚 */
  /* 嵌进桌面壳时（?embed=1）：藏掉顶栏那条，面板吃满整屏 —— 壳那边用**原生菜单**切页签
     （2026-10-10：这是"网页顶部"的最后一块，藏了之后窗口里就是纯工作区） */
  /* 切页签的进场：很短、只是"落下来一点点"，不做花哨的 */
  @keyframes shugeFadeIn { from { opacity:0; transform:translateY(4px) } to { opacity:1; transform:none } }
  .view.fade-in{animation:shugeFadeIn .14s ease-out}
  @media (prefers-reduced-motion: reduce){ .view.fade-in{animation:none} }
  /* 设置页（panelSettings）：现代软件那种"分组 + 行 + 右侧控件" */
  .setgrid{max-width:760px}
  .setcard{background:var(--panel2);border:1px solid var(--line2);border-radius:10px;padding:6px 16px 12px;margin-bottom:12px}
  .setcard h3{font-size:13px;color:var(--dim);font-weight:normal;letter-spacing:.4px;margin:14px 0 6px}
  .srow{display:flex;align-items:center;gap:14px;padding:11px 0;border-top:1px solid var(--line3)}
  .srow:first-of-type{border-top:none}
  .srow .st{flex:1;min-width:0}
  .srow .st b{font-weight:normal;color:var(--fg)}
  .srow .st i{display:block;font-style:normal;color:var(--dim);font-size:12px;margin-top:2px}
  .srow .sc{flex:0 0 auto;display:flex;gap:8px;align-items:center}
  .seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
  .seg button{border:none;background:var(--panel3);color:var(--fg2);padding:5px 12px;font-size:12.5px;cursor:pointer}
  .seg button.on{background:var(--primary);color:var(--on-primary);border-color:var(--primary-line);font-weight:bold}
  .seg button .ti{font-size:13px;line-height:1;margin-right:4px}
  .seg button .tt{font-size:12.5px}
  .setbtn{border:1px solid var(--line);background:var(--panel3);color:var(--fg2);border-radius:8px;
    padding:5px 12px;font-size:12.5px;cursor:pointer}
  .setbtn:hover{background:var(--hover2)}
  .facts{margin-top:2px}
  .fact{display:flex;gap:12px;padding:6px 0;border-top:1px solid var(--line3);font-size:12.5px}
  .fact:first-child{border-top:none}
  .fact .fk{flex:0 0 84px;color:var(--dim)}
  .fact .fv{color:var(--fg2);word-break:break-all}
  .fact .fv .warn{color:var(--amber);font-size:12px}
  body.embed .tabs{display:none}
  body.embed .panelHost{height:calc(100vh - 20px)}
  .panelHost{width:100%;height:calc(100vh - 45px);overflow:auto;background:var(--panel);position:relative;
    display:flex;flex-direction:column;min-height:0}
  .panelHost[data-loading]::after{content:"正在装配这一页…";position:absolute;top:8px;left:12px;font-size:12px;color:var(--dim)}
  .panelHost[data-error]::after{content:"这一页装配失败（看 data/logs/host.log）";position:absolute;top:8px;left:12px;font-size:12px;color:var(--red)}
  /* 2026-10-06 辞玖：「主程序、内核的页签没了」—— embed（嵌进 8795 左栏）时原本把页签藏了，
   现在**保留**：页签就是"控制台/库/内核"三个视图的入口，藏了就没法在主程序页里切换。 */
  /* 窗口版：左边那一列页签是**壳**画的（56px 宽），页面这边留出呼吸位，
     免得正文贴着那一列（辞玖 2026-10-11：贴太紧，看着像没占位） */
  body.embed .wrap{max-width:none;padding:10px 14px 10px 18px}
  body.embed .hd,body.embed .foot{display:none}
/* shuge-theme */
html[data-theme="light"] {
  --bg: #FBF6E7;
  --panel: #FFFCF3;
  --panel2: #FEF8EA;
  --panel3: #F5EDD9;
  --bar: #F7F0DE;
  --line: #E2D8BC;
  --line2: #EDE6D2;
  --fg: #33302A;
  --fg2: #4C473C;
  --dim: #6E6656;
  --blue: #2F6DB0;
  --green: #37703E;
  --red: #A8463F;
  --amber: #8A6110;
  --purple: #6A4FA3;
  --sel: #EFE3C2;
  --hover: #F6EEDA;
  --hit: #F8F2E2;
  --edge: #F3EAD3;
  --bg2: #F4EBD6;
  --bg3: #F1E7CF;
  --sel2: #DCE7F5;
  --sel3: #C9D8EE;
  --info-bg: #DCE8F7;
  --ok-bg: #DDEBD9;
  --ok-bg2: #DDEBD9;
  --warn-bg: #F6E7C6;
  --warn-bg2: #F6E7C6;
  --warn-bg3: #F7EAD0;
  --bad-bg: #F5DEDC;
  --ok-bg3: #E2EFD9;
  --ok-bg4: #D5E8CE;
  --dim2: #6E6656;
  --fg3: #3E3A31;
  --cyan: #1F6E8C;
  --blue2: #2F6DB0;
  --pink: #A8463F;
  --red2: #A8463F;
  --magenta: #8A3F7A;
  --amber2: #8A6110;
  --brown: #7A5A20;
  --brown2: #7A5A20;
  --green2: #37703E;
  --ok-fg: #2F5A33;
  --primary: #D7E5F8;
  --primary-line: #5E8CBE;
  --hover2: #F1E7CF;
  --warn-fg2: #7A5A20;
  --warn-bg4: #F6E7C6;
  --line3: #E2D8BC;
  --bg4: #F2E8D2;
  --bg5: #F1E7CF;
  --on-primary: #1F4E79;
}
/* shuge-theme */
button.theme-btn {
  display: inline-flex; align-items: center; gap: 6px;
  background: var(--panel3); color: var(--dim2);
  border: 1px solid var(--line2); border-radius: 999px;
  padding: 4px 12px; font-size: 12px; line-height: 1.5; cursor: pointer;
  white-space: nowrap;
}
button.theme-btn .ti { font-size: 13px; line-height: 1; opacity: .95; }
button.theme-btn .tt { color: var(--dim2); }
button.theme-btn:hover:not(:disabled) {
  color: var(--fg2); border-color: var(--line); background-image: none; box-shadow: none;
}
button.theme-btn:active:not(:disabled) { transform: translateY(1px) scale(.98); animation: none; }
button.theme-btn:focus-visible { outline: 2px solid var(--primary-line); outline-offset: 1px; }
@media (prefers-reduced-motion: reduce) { button.theme-btn:active:not(:disabled) { transform: none; } }
@media (prefers-reduced-motion: reduce) { button.theme-btn:active:not(:disabled) { transform: none; } }
@media (prefers-reduced-motion: reduce) { button.theme-btn:active:not(:disabled) { transform: none; } }
</style>
</head>
<body>
<div class="tabs">
  <span class="brand">🏛 枢阁</span>
  <button id="btnTheme" class="theme-btn" title="主题：浅白·护眼（带一点暖黄，看着不刺眼）/ 深色；三个界面一起变，选择会记住" onclick="__shugeTheme.toggle()"><span class="ti">☀️</span><span class="tt">浅白·护眼</span></button>
  <button class="tab active" id="tab-console" onclick="showTab('console')">🏛 控制台</button>
  <button class="tab" id="tab-notes" onclick="showTab('notes')">📚 库<span class="td" id="dotNotes">·</span></button>
  <button class="tab" id="tab-core" onclick="showTab('core')">🔧 内核<span class="td" id="dotCore">·</span></button>
  <span style="flex:1"></span>
  <span class="pill ok" id="hostPill">运行中</span>
</div>

<div class="view active" id="view-console">
<div class="wrap">
  <!-- ① 整体生命 -->
  <div class="card main">
    <div class="hd2">① 整体生命 <span class="tag" id="lifeTag">读取中…</span></div>
    <div class="row1"><span class="dot" id="dot"></span><span class="state" id="state">读取中…</span></div>
    <div class="meta" id="meta"></div>
    <div class="kids" id="kids"></div>
    <div class="btns">
      <button class="big stop" id="btnOff" onclick="powerOff()">⏻ 关闭枢阁</button>
    </div>
    <div class="note" id="note">关闭 = 停库 → 停内核 → 主进程退出，之后不再自动打开；重新打开用桌面图标「★启动枢阁」。</div>
  </div>

  <div id="patrolBar" class="patrol">巡检状态读取中…</div>
  <div class="card warn" id="warnBox" style="display:none"></div>

  <!-- ④ 日志（并入控制台） -->
  <div class="card" id="logCard">
    <div class="hd2">② 📜 日志 <span class="tag" id="logTag">—</span>
      <button class="mini" onclick="tickLogs()" style="float:right">↻ 立即刷新</button>
    </div>
    <div class="log-wrap">
      <div class="log-side" id="logSources"><div class="log-src active"><div class="nm">读取来源…</div></div></div>
      <div class="log-main">
        <div class="log-bar">
          <label>行数 <select id="logTail" onchange="logSetTail(this.value)">
            <option value="200">200</option><option value="500" selected>500</option><option value="1000">1000</option>
          </select></label>
          <label>级别 <select id="logLevel" onchange="logSetLevel(this.value)">
            <option value="">全部</option><option value="INFO">INFO</option><option value="WARN">WARN</option><option value="ERROR">ERROR</option>
          </select></label>
          <input id="logKw" placeholder="关键字过滤…" oninput="logSetKw(this.value)">
          <label><input type="checkbox" id="logAuto" checked onchange="logSetAuto(this.checked)"> 自动刷新</label>
          <span id="logInfo" style="color:var(--dim);font-size:12px"></span>
        </div>
        <div class="log-stream" id="logStream"></div>
      </div>
    </div>
  </div>

  <!-- ⑤ 平台自检 -->
  <div class="card" id="checkCard">
    <div class="hd2">③ 🩺 平台自检 <span class="tag" id="checkSummary">未运行</span>
      <button class="mini" onclick="runCheck()" style="float:right">重新自检</button></div>
    <div class="checklist" id="checkList">点右上「重新自检」：账本连续性 / 探针 / 单实例锁 / 熔断 / 端口分层 / 前门代理 / owner 登记 / 日志错误…</div>
  </div>

  <div class="foot">
    <b>枢阁</b> = 主进程（本页面，常驻、不依赖内核与库）<br>
    看笔记点上面「📚 库」，管内核点「🔧 内核」（内核的启停在那页顶部的小面板里）<br>
    <b>库永远在线</b>，没有启停这回事；内核默认开启并自动重连。
  </div>
</div>
</div>

<!-- 📚 库（笔记本体）与 🔧 内核界面：**不再是 iframe**，是把那一页的 HTML 装配进来（2026-10-10）
     好处：没有嵌套文档、没有跨文档通信、路由走 history（地址栏不跳 #），也就没有"网页感" -->
<div class="view" id="view-notes"><div class="panelHost" id="panelNotes" data-src="/notes/"></div></div>
<div class="view" id="view-core"><div class="panelHost" id="panelCore" data-src="/core/"></div></div>
<div class="view" id="view-settings"><div class="panelHost" id="panelSettings" data-src="/settings/"></div></div>

<script>
function $(id){ return document.getElementById(id); }
// esc：所有 innerHTML 拼接都要过它（页面里没有外部依赖，自己带）。
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
if (location.search.indexOf('embed=1') >= 0) { document.body.className = 'embed'; }

var ST = null;   // 最近一次 /api/host/status

// ── 三个标签页：控制台 / 库 / 内核（同页切换，iframe 首次切到才加载）──
function ensureFrame(id, url){
  var f = $(id);
  if (f && !f.getAttribute('src')) f.setAttribute('src', url);
}
// ⚠️ 这个全局名字必须**带前缀**：本页会跟"装配进来的库页/内核页"共用同一个全局作用域，
//    而库页里有一句 let TABS = []（顶层）—— 同一作用域里 var TABS 与 let TABS 撞上就是
//    SyntaxError，库页那 187KB 脚本**整段不执行**（openBin/loadTree 全没定义；
//    界面看着"面板装好了"，其实里面是死的）。2026-10-12 踩：现象是"点回收站只切页、打不开"。
var HOST_TABS = ['console', 'notes', 'core', 'settings'];
// 当前真正显示的是哪一格。它的用处只有一个：**轮询只在控制台可见时跑**。
// 以前是"不管看哪一格，每 3 秒问一次主进程状态、每 20 秒问一次巡检"，后台一直有动静；
// 2026-10-12 对照 Obsidian（初始化几秒，之后什么都不再发生）改成按需。
var CUR_TAB = 'console';
function showTab(name){
  if (HOST_TABS.indexOf(name) < 0) name = 'console';
  CUR_TAB = name;
  HOST_TABS.forEach(function(n){
    var v = $('view-' + n), t = $('tab-' + n);
    if (v) v.className = 'view' + (n === name ? ' active' : '');
    if (t) t.className = 'tab' + (n === name ? ' active' : '');
  });
  // 切页签的过渡：给刚显示的那一格一个进场动画（CSS 里 140ms；尊重"减少动效"的系统设置）
  try {
    var now = $('view-' + name);
    if (now && !REDUCED) { now.classList.remove('fade-in'); void now.offsetWidth; now.classList.add('fade-in'); }
  } catch (e) {}
  // 面板按需装配（首次切到才拉那一页的 HTML；之后只切显示）
  if (name === 'notes') loadPanel('panelNotes');
  if (name === 'core') loadPanel('panelCore');
  if (name === 'settings') loadPanel('panelSettings');
  if (name === 'console') {
    if (LOG.auto) { startLogAuto(); tickLogs(); }
    load(); loadPatrol();                       // 切回来立刻对表，不等下一次轮询
  } else { stopLogAuto(); }
  // 路由：用 history 而不是 hash（地址栏不再跳 #，也就没有"网页翻页"的感觉）
  try {
    var want = '/console' + (name === 'console' ? '' : '/' + name);
    if (location.pathname + location.hash !== want) history.pushState({ tab: name }, '', want);
  } catch (e) {}
}
var REDUCED = false;
try { REDUCED = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); } catch (e) {}
window.showTab = showTab;
// 回退/前进（popstate）：从路径里认页签；老书签里的 #notes 也照认（向后兼容）
window.addEventListener('popstate', function(){
  var p = (location.pathname || '').replace(/\/+$/, '');
  var n = p.indexOf('/notes') >= 0 ? 'notes'
        : (p.indexOf('/core') >= 0 ? 'core'
        : (p.indexOf('/settings') >= 0 ? 'settings' : 'console'));
  showTab(n, true);
});


function fmtUp(s){
  s = Math.max(0, parseInt(s || 0, 10));
  if (s < 60) return s + ' 秒';
  if (s < 3600) return Math.floor(s/60) + ' 分 ' + (s%60) + ' 秒';
  return Math.floor(s/3600) + ' 小时 ' + Math.floor((s%3600)/60) + ' 分';
}
function tag(el, text, cls){ if (!el) return; el.textContent = text; el.className = 'tag ' + (cls || ''); }
function dot(el, cls){ if (el) el.className = 'dot ' + (cls || ''); }
function autoLabel(on){ return on ? '自动重连：开 ✓' : '自动重连：关 ✗'; }
// 采样时刻：状态是缓存的（几秒），就如实写出来 —— 看到的是真采到的，取样时间写在旁边
function sampled(at){
  if (!at) return '';
  var sec = Math.max(0, Math.round((Date.now() - new Date(at).getTime()) / 1000));
  return sec <= 1 ? '采样：刚刚' : ('采样：' + sec + ' 秒前');
}
function autoCls(on){ return on ? '' : ' style="border-color:var(--red)55;color:var(--red)"'; }

// ── ① 整体生命 ─────────────────────────────────────────────
function render(st){
  ST = st;
  var paused = !!st.host_paused;
  var h = st.host_pid, up = fmtUp(st.uptime_s), port = st.port;
  $('state').textContent = paused ? '已人工关闭（不会自动打开）' : '枢阁运行中';
  dot($('dot'), paused ? 'warn' : 'ok');
  tag($('lifeTag'), paused ? '人工关闭' : '运行中', paused ? 'warn' : 'ok');
  $('hostPill').className = 'pill ' + (paused ? 'bad' : 'ok');
  $('hostPill').textContent = paused ? '已关闭' : '运行中';

  var bits = ['主进程 pid ' + h + ' · 已运行 ' + up + ' · 端口 ' + port];
  if (st.host_state) {
    var m = {healthy:'正常', paused:'已人工停止（不自动拉起）', quarantined:'已熔断隔离',
             down:'对象不在（正在拉起）', starting:'对象启动中', 'serve-up':'主进程就绪'};
    bits.push('看护状态：' + (m[st.host_state] || st.host_state));
  }
  if (st.owner && st.owner.launched_by) bits.push('内核上次启动者：' + st.owner.launched_by);
  $('meta').innerHTML = bits.map(function(b){ return '<div>' + esc(b) + '</div>'; }).join('');

  var n = st.notes || {}, c = st.core || {};
  var nOn = !!n.healthy, cOn = !!(c && c.online);
  if ($('dotNotes')) $('dotNotes').textContent = nOn ? '🟢' : '⚫';
  if ($('dotCore')) $('dotCore').textContent = cOn ? '🟢' : '⚫';
  $('kids').innerHTML =
    '<div class="k" onclick="showTab(\'notes\')" style="cursor:pointer">📚 库 '
      + (nOn ? '🟢 在线' : (n.port_open ? '🟠 端口在、不答话（可能在忙）' : '⛔ 不在（正在拉起）')) + ' ↗</div>'
    + '<div class="k" onclick="showTab(\'core\')" style="cursor:pointer">🔧 内核 '
      + (cOn ? '🟢 在跑' : '⛔ 未运行') + ' · ' + autoLabel(st.core_autoreconnect)
      + (st.core_checked_at ? ' · 采样：' + sampled(st.core_checked_at).replace('采样：', '') : '') + ' ↗</div>';

  var warns = [];
  if (paused) warns.push('⏸ 已置「人工关闭」（data/host.paused）：计划任务不会再把枢阁拉起来；桌面图标可重新打开。');
  if (st.core_paused && cOn) warns.push('ℹ 内核已关「自动重连」：它现在还跑着，但挂了不会自动拉起。');
  if (st.quarantine) warns.push('⛔ 内核已被熔断隔离（10 分钟内自动拉起次数过多）。看日志定位，再点「重启内核」。');
  if (!cOn && st.lock_busy) warns.push('⚠ 有内核进程持有单实例锁但健康检查不通：可能正在启动，也可能卡住了。');
  var wb = $('warnBox');
  wb.style.display = warns.length ? 'block' : 'none';
  wb.innerHTML = warns.map(function(w){ return '<div>' + esc(w) + '</div>'; }).join('');

}

// ── 动作 ──────────────────────────────────────────────────
function act(url, body, done){
  var opt = { method: 'POST' };
  if (body) { opt.headers = { 'Content-Type': 'application/json' }; opt.body = JSON.stringify(body); }
  fetch(url, opt).then(function(r){ return r.json(); }).then(function(d){
    if (done) done(d);
    setTimeout(load, 600); setTimeout(load, 2500);
  }).catch(function(e){ $('note').textContent = '请求失败：' + e; });
}

function powerOff(){
  if (!confirm('关闭枢阁？\n\n· 先停库（笔记页面会不可用）、再停内核\n· 之后**不再自动打开**（计划任务也会被拦住）\n· 重新打开：桌面图标「★启动枢阁」')) return;
  $('note').textContent = '正在关闭：停库 → 停内核 → 主进程退出…';
  act('/api/host/power/off', null, function(d){
    $('note').textContent = (d && d.ok === false) ? ('没执行：' + (d.err || '')) : '已关闭。这个页面已经失去连接，可以关掉了；要用请点桌面图标重新打开。';
  });
}

// ── 巡检横条 ──────────────────────────────────────────────
var PATROL_ICON = {ok:'🟢', warn:'🟠', fail:'🔴', unknown:'⚪'};
function loadPatrol(){
  var bar = $('patrolBar');
  if (!bar) return;
  fetch('/api/host/patrol', {cache:'no-store'}).then(function(r){ return r.json(); }).then(function(p){
    var ic = PATROL_ICON[p.verdict] || '⚪';
    bar.className = 'patrol ' + (p.verdict || 'unknown');
    var when = p.last_at ? new Date(p.last_at).toLocaleTimeString() : '还没跑过';
    var every = p.every_s ? Math.round(p.every_s/60) + ' 分钟' : '—';
    var parts = [ic + ' <b>常驻巡检</b>',
      '每 ' + every + ' 一次 · 上次 ' + when + (p.total_runs ? '（第 ' + p.total_runs + ' 次）' : ''),
      p.summary ? esc(p.summary) : '尚未出结论'];
    if (p.known) parts.push('<span class="t">已知 ' + p.known + ' 项（例如历史账本损坏，已记基线）</span>');
    if (p.problems && p.problems.length) {
      parts.push('<span>异常：' + p.problems.map(esc).join('、') + '</span>');
    }
    var last = (p.transitions || [])[0];
    if (last) parts.push('<span class="t">最近跃迁 ' + new Date(last.at).toLocaleString() + '：' + esc(last.kind) + '</span>');
    bar.innerHTML = parts.join(' <span class="t">·</span> ');
  }).catch(function(){ bar.className = 'patrol'; bar.textContent = '巡检状态读取失败（主进程在？）'; });
}

// ── ④ 日志 ────────────────────────────────────────────────
var LOG = { source: '__all__', tail: 500, kw: '', level: '', auto: true, timer: null, sources: [], kernelUp: false, sourcesAt: 0 };
var LINE_RE = /^(\S+)\s*\|\s*(\S+)\s*\|\s*([^|]*?)\s*\|\s*([\s\S]*)$/;
function parseLine(line){
  var m = String(line).match(LINE_RE);
  if (!m) return { ts:'', level:'', source:'', text:String(line) };
  return { ts:m[1], level:m[2].trim(), source:m[3].trim(), text:m[4] };
}
function fmtSize(n){
  n = n || 0;
  if (n < 1024) return n + ' B';
  if (n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
  return (n/1024/1024).toFixed(1) + ' MB';
}
function hhmmss(ts){ var m = String(ts||'').match(/T(\d{2}:\d{2}:\d{2})/); return m ? m[1] : String(ts||''); }

function loadLogSources(force){
  // 来源清单变化很慢（只有新插件/新日志文件才会变），30 秒问一次足够；
  // 2026-10-12 之前每刷一次日志就问一次（每 5 秒一笔），纯浪费。
  var now = Date.now();
  if (!force && LOG.sourcesAt && (now - LOG.sourcesAt) < 30000) return Promise.resolve();
  return fetch('/api/host/logsources', {cache:'no-store'}).then(function(r){ return r.json(); }).then(function(d){
    LOG.sourcesAt = now;
    LOG.sources = (d && d.sources) || [];
    LOG.kernelUp = !!(d && d.kernel_up);
    renderLogSources();
  });
}
function sourceRow(id, label, meta, cls){
  var active = (LOG.source === id) ? ' active' : '';
  return '<div class="log-src' + active + '" onclick="selectLogSource(\'' + esc(id) + '\')">'
    + '<div class="nm">' + esc(label) + '</div><div class="meta">' + esc(meta || '') + '</div></div>';
}
function renderLogSources(){
  var box = $('logSources');
  if (!box) return;
  // 内核不在 → 平台事件这个来源**不出现**（而不是出现了再解释一句）
  if (LOG.source === '__events__' && !LOG.kernelUp) LOG.source = '__all__';
  var total = 0;
  LOG.sources.forEach(function(s){ total += (s.bytes || 0); });
  var rows = [sourceRow('__all__', '全部（聚合时间线）', fmtSize(total) + ' · ' + LOG.sources.length + ' 个来源')];
  if (LOG.kernelUp) rows.push(sourceRow('__events__', '⚡ 平台事件（总线·账本）', '账本里的业务事件'));
  LOG.sources.forEach(function(s){ rows.push(sourceRow(s.source, s.name || s.source, fmtSize(s.bytes) + ' · ' + hhmmss(s.updated))); });
  box.innerHTML = rows.join('');
}
window.selectLogSource = function(id){ LOG.source = id; renderLogSources(); tickLogs(); };

function logSetTail(v){ LOG.tail = parseInt(v,10) || 500; tickLogs(); }
function logSetLevel(v){ LOG.level = v; tickLogs(); }
function logSetKw(v){ LOG.kw = (v||'').toLowerCase(); tickLogs(); }
function logSetAuto(on){ LOG.auto = !!on; if (on) startLogAuto(); else stopLogAuto(); }
function startLogAuto(){ if (LOG.timer) return; LOG.timer = setInterval(tickLogs, 5000); }
function stopLogAuto(){ if (LOG.timer) { clearInterval(LOG.timer); LOG.timer = null; } }

function fetchSource(id){
  return fetch('/api/host/log?source=' + encodeURIComponent(id) + '&tail=' + LOG.tail, {cache:'no-store'})
    .then(function(r){ return r.json(); });
}
function loadLogStream(){
  var box = $('logStream');
  if (!box) return Promise.resolve();
  var jobs;
  if (LOG.source === '__all__') {
    if (!LOG.sources.length) { box.innerHTML = '<div class="log-line"><span class="tx">（还没有日志来源）</span></div>'; return Promise.resolve(); }
    // 聚合交给主进程：**一笔**换回全部来源的合并时间线。
    // 2026-10-12：原来在浏览器里 forEach 每个来源各发一笔（库里 30 多个来源 → 每 5 秒 30 多笔），
    // 换成 /api/host/log/timeline 后每 5 秒 1 笔；口径与原来一致（各来源先取 tail/来源数，再按 ts 倒序）。
    jobs = fetch('/api/host/log/timeline?tail=' + LOG.tail, {cache:'no-store'})
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (d && d.ok === false) { $('logInfo').textContent = d.err || '读取失败'; return []; }
        return (d && d.lines) || [];
      })
      .catch(function(){ return []; });
  } else {
    jobs = fetchSource(LOG.source).then(function(d){
      if (d && d.ok === false) { $('logInfo').textContent = d.err || '读取失败'; return []; }
      return (d && d.lines) || [];
    });
  }
  return jobs.then(function(lines){ renderLogLines(lines); });
}
function renderLogLines(lines){
  var box = $('logStream');
  if (!box) return;
  var lvColor = { INFO:'var(--cyan)', WARN:'var(--amber)', ERROR:'var(--red)' };
  var shown = 0, out = [];
  lines.forEach(function(raw){
    var p = parseLine(raw);
    if (LOG.level && p.level !== LOG.level) return;
    if (LOG.kw && String(raw).toLowerCase().indexOf(LOG.kw) < 0) return;
    shown++;
    out.push('<div class="log-line"><span class="t">' + esc(hhmmss(p.ts) || p.ts) + '</span>'
      + '<span class="lv" style="color:' + (lvColor[p.level] || 'var(--fg2)') + '">' + esc(p.level) + '</span>'
      + '<span class="src">' + esc(p.source) + '</span>'
      + '<span class="tx">' + esc(p.text) + '</span></div>');
  });
  box.innerHTML = out.length ? out.join('') : '<div class="log-line"><span class="tx">（没有匹配的日志行）</span></div>';
  $('logInfo').textContent = '显示 ' + shown + ' / ' + lines.length + ' 行 · ' + new Date().toLocaleTimeString();
  var tagEl = $('logTag');
  if (tagEl) { tagEl.textContent = (LOG.source === '__events__' ? '平台事件' : LOG.source === '__all__' ? '全部' : LOG.source); tagEl.className = 'tag'; }
}
window.tickLogs = function(){
  return loadLogSources().then(loadLogStream).catch(function(){ /* 日志区不打扰主流程 */ });
};

// ── ⑤ 自检 ────────────────────────────────────────────────
var CK_ICON = {ok:'✅', known:'📌', warn:'⚠️', fail:'❌', skip:'⏭️'};
function runCheck(){
  var sum = $('checkSummary');
  if (sum) { sum.textContent = '检查中…'; sum.className = 'tag'; }
  fetch('/api/host/check', {cache:'no-store'}).then(function(r){ return r.json(); }).then(function(d){
    if (sum) {
      sum.textContent = d.summary;
      sum.className = 'tag ' + (d.fail ? 'bad' : (d.warn ? 'warn' : 'ok'));
    }
    $('checkList').innerHTML = d.items.map(function(it){
      return '<div class="ck ' + it.status + '">'
        + '<span class="ic">' + (CK_ICON[it.status] || '·') + '</span>'
        + '<span class="nm">' + esc(it.name) + '</span>'
        + '<span class="dt">' + esc(it.detail) + '</span>'
        + (it.hint ? '<span class="ht">↳ ' + esc(it.hint) + '</span>' : '')
        + '</div>';
    }).join('');
  }).catch(function(e){ if (sum) sum.textContent = '自检失败：' + e; });
}

// ── 回收站：壳上那一列最底下那颗按钮叫它 ──────────────────────────
// 为什么放在页面里（而不是壳里定时戳）：页面才知道自己哪一格能开回收站、面板装配完没有。
// 以前壳里是"切到库 + 等 900ms 再调 openBin()"—— 面板还没装好时 openBin 根本不存在，
// 于是只看到"跳到了库页"（辞玖 2026-10-12 报的就是这个）。
window.__shugeOpenBin = function () {
  function back(ok, why) {
    try {
      if (window.chrome && chrome.webview && chrome.webview.postMessage) {
        chrome.webview.postMessage(JSON.stringify({ shuge: ok ? 'binOpened' : 'binFailed', why: why || '' }));
      }
    } catch (e) {}
    return (ok ? 'opened:' : 'failed:') + (why || '');
  }
  // ① 当前这一格就能开（库页的 openBin）
  try { if (typeof openBin === 'function' && document.getElementById('view-notes') &&
             document.getElementById('view-notes').className.indexOf('active') >= 0) { openBin(); return back(true, 'notes'); } } catch (e) {}
  // ② 内核那一格有自己的回收站
  try {
    var vc = document.getElementById('view-core');
    if (typeof go === 'function' && vc && vc.className.indexOf('active') >= 0) { go('trash'); return back(true, 'core'); }
  } catch (e) {}
  // ③ 其余情况：切到库，**等它的面板真的装好**再开（不赌时间）
  showTab('notes');
  var tries = 0;
  var t = setInterval(function () {
    tries++;
    if (typeof openBin === 'function') {
      clearInterval(t);
      try { openBin(); } catch (e) { back(false, 'openBin 抛错：' + e); return; }
      back(true, 'notes-late');
      return;
    }
    if (tries > 60) { clearInterval(t); back(false, '库页没装配出来（60×300ms）'); }
  }, 300);
  return 'waiting';
};

// ── 主循环 ────────────────────────────────────────────────
function load(){
  fetch('/api/host/status', {cache:'no-store'}).then(function(r){ return r.json(); }).then(render).catch(function(){
    $('hostPill').className = 'pill bad';
    $('hostPill').textContent = '主进程异常';
    $('state').textContent = '读不到主进程状态（它可能正在关闭）';
    dot($('dot'), 'bad');
  });
}
load(); loadPatrol();
if (document.body.className.indexOf('embed') < 0) { runCheck(); }
// 嵌进桌面壳（?embed=1）：藏顶栏（页签改用壳的原生菜单）；老书签 #notes 仍认
(function(){
  try {
    var q = location.search || '';
    if (/[?&]embed=1\b/.test(q)) document.body.classList.add('embed');
  } catch (e) {}
})();
showTab((function(){
  // 老书签的 #hash 优先；其次是路径（/console/notes、/console/settings…）。
  // ⚠️ 一定要把 settings 认上：不认的话这一句会把它当控制台、顺手 pushState 回 /console，
  //    表现就是"地址自己变回 /console、设置那一格也没装上"（2026-10-12 实测）。
  var h = (location.hash || '').replace('#', '');
  if (h && HOST_TABS.indexOf(h) >= 0) return h;
  var p = (location.pathname || '');
  if (p.indexOf('/notes') >= 0) return 'notes';
  if (p.indexOf('/core') >= 0) return 'core';
  if (p.indexOf('/settings') >= 0) return 'settings';
  return 'console';
})());
// 轮询：**只在控制台那一格可见时**跑（别的格子不需要它，也就别在后台一直敲主进程）。
// 巡检本身是每几分钟才跑一次的常驻任务，20 秒问一次纯属浪费 → 60 秒。
setInterval(function(){ if (CUR_TAB === 'console' && !document.hidden) load(); }, 3000);
setInterval(function(){ if (CUR_TAB === 'console' && !document.hidden) loadPatrol(); }, 60000);

// ── 预热：把「库」那一格在后台先装好（2026-10-12，对照 Obsidian）──
// Obsidian 是"启动时把库索引建完（几秒），之后打开任何东西都不再等"。
// 我们原来是"点到「库」才开始拉那一页 HTML + 执行那 187KB 脚本 + 建 5472 个节点"，
// 第一次点要等 350ms 左右、还看得见「加载中…」。现在控制台一显示出来就在后台装好，
// 切过去就只是显示/隐藏（那些时间挪进启动的头一秒里，人也看不见）。
var WARMED = false;
function warmPanels(){
  if (WARMED) return;
  WARMED = true;
  // 只预热「库」：loadPanel 会把"请求前缀转发"的当前面板设成它，而 panelNotes 是有转发规则的
  // （/notes）。**不能**把当前面板留在内核或设置上 —— 那两个没规则，库页自己那些 /api/... 就没人加前缀了。
  try { loadPanel('panelNotes'); } catch (e) {}
}
setTimeout(warmPanels, 700);
</script>










<script>/* shuge-theme */
/* shuge-theme —— 枢阁的统一主题引导（浅白·护眼 / 深色）
 *
 * 辞玖 2026-10-06：「所有页面主题统一」+「图片中的问题是只页签能切换主题，其他地方没变」。
 *
 * 为什么要有这个文件（而不是各页面各写一小段）：
 *   · 三个界面（主程序控制台 8790 / 内核控制台 8795 / 库 8848）**端口不同 = 不同源**，
 *     localStorage 各存各的 → 必须有一份**服务端的偏好**；
 *   · 主程序（8790）是唯一地址，它的 「/api/ui/theme」 就是那份真相（本机来源放行，见 isLocalOrigin）；
 *   · 「只页签变了、其他地方没变」是因为库/内核是**同页 iframe**，外层改主题不会动到 iframe 里的文档
 *     → 切换时**主动推进 iframe**（同源直接写它的 <html>，跨端口再 postMessage 一次）；
 *   · 被别页嵌着时（主程序页嵌在内核控制台左栏）**跟随父页面**，并且不吃掉父页面的设置。
 *
 * 规则（避免互相打架/死循环）：
 *   · 只有**用户点击**才写 localStorage / POST 服务端 / 推给子 iframe / 通知同源标签页；
 *   · 从服务端、父页面、postMessage / BroadcastChannel 来的主题一律 「apply(t, true)」：只应用，不再回推
 *     （2026-10-06 事故：这里原来无条件 push，两个页面无限互推 →「两个主题自己频繁变化」）；
 *   · **实时**（辞玖 2026-10-06「全实时」）：连主程序（8790）的 SSE 地址 /api/ui/theme/stream，
 *     谁一改立刻推给所有页面；拿不到 SSE 就退回每 3 秒轮询（保底，不赌）；
 *   · 同一个源开多个标签页时，BroadcastChannel 立刻同步（storage 事件在某些情况下不投递，只当兜底）。
 */
(function () {
  var KEY = 'shuge-theme';
  var API = 'http://127.0.0.1:8790/api/ui/theme';   // 主程序（唯一地址）
  var STREAM = API + '/stream';                      // 同一条路的 SSE：主题一变就推过来（全实时）
  var POLL_MS = 3000;                                // 拿不到 SSE 时的保底轮询间隔（只在页面可见时跑）
  var USE_API = true;                                // 拿不到就静默退回 localStorage

  function norm(t) { return (t === 'light') ? 'light' : 'dark'; }

  // 按钮文字：两个 span（图标 + 文字），别用 textContent 整块覆盖（否则 icon 一起被抹掉）
  function label(t) {
    var b = document.getElementById('btnTheme');
    if (!b) return;
    var ic = b.querySelector ? b.querySelector('.ti') : null;
    var tx = b.querySelector ? b.querySelector('.tt') : null;
    var on = (t === 'light');
    if (ic) ic.textContent = on ? '🌙' : '☀️';
    if (tx) tx.textContent = on ? '深色' : '浅白·护眼';
    if (!ic && !tx) b.textContent = on ? '🌙 深色' : '☀️ 浅白·护眼';   // 老页面（没有两个 span）兜底
    b.title = on ? '当前浅白·护眼，点一下换深色（三个界面一起变）' : '当前深色，点一下换浅白·护眼（三个界面一起变）';
  }

  function frames() {
    var out = [];
    try {
      var fs = document.querySelectorAll('iframe');
      for (var i = 0; i < fs.length; i++) out.push(fs[i]);
    } catch (e) {}
    return out;
  }

  // 推给子 iframe（库 / 内核就是同页 iframe）+ 通知父页面
  function push(t) {
    frames().forEach(function (f) {
      try {
        var d = f.contentDocument;                  // 同源：直接写它的 <html>
        if (d) {
          d.documentElement.setAttribute('data-theme', t);
          var w = f.contentWindow;
          if (w && w.__shugeTheme && w.__shugeTheme.apply) w.__shugeTheme.apply(t, true);
        }
      } catch (e) {}                                 // 跨源拿不到 document：忽略，下面用 postMessage
      try { f.contentWindow.postMessage({ shugeTheme: t }, '*'); } catch (e) {}
    });
    try { if (window.parent && window.parent !== window) window.parent.postMessage({ shugeTheme: t }, '*'); } catch (e) {}
  }

  // 同一个**源**的别的页面（比如 8790 开两个标签）：BroadcastChannel 立刻通知它们。
  // ⚠️ 实测（2026-10-06，Edge 153 无头）：跨"端口"的标签**收不到** BroadcastChannel，localStorage 也各存各的
  //   —— 因为 8790 / 8795 / 8848 端口不同 = 三个不同的源。所以跨端口那一半只能靠**服务端**：
  //   点的那一页 POST 服务端，别的页面在自己**重新可见（切回来）**时拉一次（下面有 visibilitychange）。
  var chan = null;
  try { if (typeof BroadcastChannel !== 'undefined') chan = new BroadcastChannel('shuge-theme'); } catch (e) {}
  function tell(t) {
    try { if (chan) chan.postMessage({ shugeTheme: t }); } catch (e) {}
  }

  function save(t) {
    if (!USE_API) return;
    try {
      fetch(API, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ theme: t })
      }).catch(function () {});
    } catch (e) {}
  }

  function pull(then) {
    if (!USE_API) return;
    try {
      fetch(API, { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d && d.theme) then(norm(d.theme)); })
        .catch(function () {});
    } catch (e) {}
  }

  // 实时同步：SSE 为主，轮询兜底。两条路都只调 apply(t, true)（收到别人的主题不再往外推）
  function live() {
    if (!USE_API) return;
    var lastAsk = 0;
    function ask() {                                 // 兜底：直接问一次服务端（带 1.5 秒节流）
      var now = Date.now();
      if (now - lastAsk < 1500) return;
      lastAsk = now;
      pull(function (remote) { if (norm(remote) !== get()) apply(remote, true); });
    }
    var es = null;
    try { if (typeof EventSource !== 'undefined') es = new EventSource(STREAM); } catch (e) { es = null; }
    if (es) {
      es.addEventListener('theme', function (ev) {
        try {
          var d = JSON.parse(ev.data);
          if (d && d.theme && norm(d.theme) !== get()) apply(d.theme, true);
        } catch (e) {}
      });
      // EventSource 自己会重连；重连成功时再对一次表，断线期间漏掉的那次不能丢
      es.addEventListener('hello', function (ev) {
        try {
          var d = JSON.parse(ev.data);
          if (d && d.theme && norm(d.theme) !== get()) apply(d.theme, true);
        } catch (e) {}
      });
      es.addEventListener('error', function () { setTimeout(ask, 800); });
    } else {
      setInterval(function () { if (!document.hidden) ask(); }, POLL_MS);
    }
    // 切回这个页面时对一次表（SSE 没连上/断线时它最有用）
    try {
      document.addEventListener('visibilitychange', function () { if (!document.hidden) ask(); });
    } catch (e) {}
  }

  function apply(t, fromElsewhere) {
    t = norm(t);
    document.documentElement.setAttribute('data-theme', t);
    label(t);
    // ⚠️ 只有"用户点的那一下"才往外写/往外推。
    // 2026-10-06 事故：这里原来无条件 push(t)，于是收到推送的一方又推回去 —— 两个页面无限互推，
    // 值不一致时就表现为「两个主题自己频繁变化」。收到别人的主题只在本页生效，链路到此为止。
    if (!fromElsewhere) {
      try { localStorage.setItem(KEY, t); } catch (e) {}
      save(t);
      push(t);
      tell(t);
    }
    return t;
  }

  function get() { return document.documentElement.getAttribute('data-theme') || 'dark'; }

  window.__shugeTheme = {
    get: get,
    apply: apply,
    set: function (t) { return apply(t, false); },
    toggle: function () { return apply(get() === 'light' ? 'dark' : 'light', false); },
    push: push,
    api: API
  };

  // 别的页面（父页面 / 内核控制台里嵌着我们）推过来的主题
  window.addEventListener('message', function (ev) {
    var d = ev && ev.data;
    if (!d || !d.shugeTheme) return;
    if (norm(d.shugeTheme) === get()) return;        // 值没变：什么都不做（幂等短路，也少一次 DOM 写）
    apply(d.shugeTheme, true);
  });

  // 同源标签页里改的主题：BroadcastChannel 为主，localStorage 的 storage 事件兜底
  try {
    if (chan) chan.addEventListener('message', function (ev) {
      var d = ev && ev.data;
      if (d && d.shugeTheme && norm(d.shugeTheme) !== get()) apply(d.shugeTheme, true);
    });
  } catch (e) {}
  window.addEventListener('storage', function (ev) {
    if (ev && ev.key === KEY && ev.newValue && norm(ev.newValue) !== get()) apply(ev.newValue, true);
  });

  function parentTheme() {
    try {
      if (window.parent && window.parent !== window && window.parent.document) {
        return window.parent.document.documentElement.getAttribute('data-theme') || '';
      }
    } catch (e) {}                                   // 跨源父页面：读不到，靠 postMessage
    return '';
  }

  function hideIfEmbedded() {
    var embedded = false;
    try { embedded = (window.self !== window.top); } catch (e) { embedded = true; }
    if (embedded) {
      var b = document.getElementById('btnTheme');
      if (b) b.style.display = 'none';               // 外层页签栏上已经有一个了，别重复
    }
  }

  function local() { try { return localStorage.getItem(KEY) || ''; } catch (e) { return ''; } }
  function query() {
    try { return new URLSearchParams(location.search).get('theme') || ''; } catch (e) { return ''; }
  }

  function boot() {
    // 先用本页能立刻拿到的：?theme= → localStorage → 父页面 → 默认深色
    // （boot 这一步**不写**服务端：否则一个默认深色的新页面会把别人设的浅色冲掉）
    apply(query() || local() || parentTheme() || 'dark', true);
    // 再问服务端（唯一真相）：拿到了就覆盖
    pull(function (remote) { apply(remote, true); });
    live();                                          // 之后就是实时的：SSE 一推就变
    hideIfEmbedded();
  }

  if (typeof document === 'undefined') return;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();</script>
<script>
// ── 设置页的活（2026-10-11）────────────────────────────────────────
// 分两类：
//   ① 页面自己能做的：主题（走同一个 __shugeTheme）、进检索库状态、版本与路径（读 /api/host/status）
//   ② 只有**窗口版**才有的：缩放 / 全屏 / 开发者工具 / 在浏览器打开 / 打开目录
//      —— 这些问壳要"桥"（WebView2 的 hostObjects）。没有桥（普通浏览器打开）就整段隐身，
//         不留"点了没反应"的按钮。
(function () {
  function $(id) { return document.getElementById(id); }
  function esc(x) { return String(x == null ? '' : x).replace(/[&<>"]/g, function (c) { return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]; }); }

  // 壳的桥：窗口版里就是 window.chrome.webview（WebView2 的消息通道）。
  // ⚠️ 别走 hostObjects/AddHostObjectToScript：本机实测注册不上（取它报 0x80070490），
  //    而它既不抛异常也不生效 —— 失败看不见。消息通道一行就通。
  function webview() {
    try {
      var c = window.chrome && window.chrome.webview;
      return (c && typeof c.postMessage === 'function') ? c : null;
    } catch (e) { return null; }
  }
  function bridge() { return webview(); }

  function paintTheme() {
    var t = (window.__shugeTheme && __shugeTheme.get()) || 'dark';
    var box = $('setTheme');
    if (!box) return;
    [].slice.call(box.querySelectorAll('button[data-t]')).forEach(function (b) {
      b.classList.toggle('on', b.getAttribute('data-t') === t);
    });
  }

  function call(name) {
    var w = webview();
    if (!w) return;
    try { w.postMessage(JSON.stringify({ shuge: name })); } catch (e) {}
  }

  // ⚠️ 这一段脚本是**跟着面板一起**被搬进文档的，可能比元素先执行（面板 HTML 很大）。
  //    所以先等"设置那几张卡片真的在文档里"再干活 —— 否则 setShellOnly / setFacts 全会找不到，
  //    表现就是"窗口那一组不显示、事实行一直读取中"（2026-10-11 踩）。
  function ready() { return !!(document.getElementById('setShellOnly') && document.getElementById('setFacts')); }
  function whenReady(fn, tries) {
    tries = tries || 0;
    if (ready()) { fn(); return; }
    if (tries > 6000) return;                          // ~10 分钟还等不到就放弃（正常 1 秒内就到）
    setTimeout(function () { whenReady(fn, tries + 1); }, 100);
  }
  try { window.addEventListener('shuge:panel-ready', function (e) {
    if (e && e.detail && e.detail.id === 'panelSettings') whenReady(function () { wire(); });
  }); } catch (e) {}

  function wire() {
    if (wire.done || !ready()) return;                 // 元素没齐就不算数，等下一拍
    wire.done = true;
    var has = false;
    try { has = !!webview(); } catch (e) {}
    var shellOnly = $('setShellOnly');
    if (shellOnly) shellOnly.style.display = has ? '' : 'none';
    var note = $('setBridgeNote');
    if (note) note.style.display = has ? 'none' : '';


    document.addEventListener('click', function (ev) {
      var el = ev.target;
      while (el && el !== document.body && !el.getAttribute) el = el.parentNode;
      if (!el || !el.getAttribute) return;
      var act = el.getAttribute('data-act');
      if (!act) return;
      ev.preventDefault();
      if (act === 'theme') { if (window.__shugeTheme) __shugeTheme.set(el.getAttribute('data-t')); paintTheme(); return; }
      if (act === 'openBin') {                                  // 回收站：库页有 openBin()，内核页是 go('trash')
        try {
          if (typeof openBin === 'function') { openBin(); return; }
          if (typeof go === 'function') { go('trash'); return; }
        } catch (e) {}
        call('openBin');                                       // 都不在（控制台那一格）→ 让壳去叫
        return;
      }
      call(act);
    });

    paintTheme();
    // 主题被别处改了（SSE 推过来）也要跟着亮
    try { window.addEventListener('shuge:theme', paintTheme); } catch (e) {}
    // 2026-10-12：原来是 setInterval(paintTheme, 1200) —— 每秒多一次的定时器永远在跑。
    // 主题最终都落在 <html data-theme> 上，所以**盯着那个属性**就够了：变一次画一次，不变不画。
    try {
      new MutationObserver(paintTheme).observe(document.documentElement,
        { attributes: true, attributeFilter: ['data-theme'] });
    } catch (e) {}
    try { loadFacts(); } catch (e) {}
  }

  // 事实行：版本 / 目录 / 库与内核的现状（读主进程自己的 /api/host/status）
  function loadFacts() {
    // 三个来源各司其职：
    //   /api/host/about  —— 「这台机器上 / 关于枢阁」要的那几行（**全从当下真实位置推**）
    //   /api/host/status —— 库/内核在不在这些活状态
    //   /healthz         —— 兜底（about 拿不到时至少还有版本）
    Promise.all([
      fetch('/api/host/status', { cache: 'no-store' }).then(function (r) { return r.json(); }),
      fetch('/healthz', { cache: 'no-store' }).then(function (r) { return r.json(); }).catch(function () { return {}; }),
      fetch('/api/host/about', { cache: 'no-store' }).then(function (r) { return r.json(); }).catch(function () { return {}; })
    ])
      .then(function (both) {
        var st = both[0], hz = both[1] || {}, ab = both[2] || {};
        var n = st.notes || {}, c = st.core || {};
        var rows = [
          ['版本', (ab.version || st.version || hz.version || '—') + '（主进程 pid ' + (ab.pid || st.host_pid || hz.pid || '?') + '）'],
          ['应用根', ab.app_root || '—'],
          ['平台目录', ab.core_dir || st.core_dir || hz.core_dir || '—'],
          ['库', (n.dir || '—') + (n.healthy ? '（在线）' : '（不在）')],
          ['内核', (c.online ? '在跑' : '未运行')],
          ['窗口', webview() ? '窗口版（这一列页签由壳提供）' : '网页版（浏览器标签）']
        ];
        var box = $('setFacts');
        if (box) box.innerHTML = rows.map(function (r) {
          return '<div class="fact"><span class="fk">' + esc(r[0]) + '</span><span class="fv">' + esc(r[1]) + '</span></div>';
        }).join('');
        loadAbout(st, hz, ab);
      })
      .catch(function () {});
  }

  // 「关于枢阁」：主进程版本 / 应用根 / 平台目录 / 库目录（含这份是从哪来的）/ 界面地址 /
  // 界面装配端口 / 用户目录 / 窗口版本 / 窗口状态文件。
  // 全是**当下真实位置**推出来的 —— 换台机器、换个目录解压，这些值跟着变。
  // 壳那边的两样（壳版本、窗口状态文件）由壳写进 data/shell-state.json，页面读同一份。
  // config.yaml 里写的库目录跟实际用的不一致时，要不要多显示一行说明
  function cfgNote(ab) {
    if (!ab || !ab.vault_config) return false;
    var a = String(ab.vault || '').toLowerCase().replace(/[\\/]+$/, '');
    var b = String(ab.vault_config).toLowerCase().replace(/[\\/]+$/, '');
    return a !== b;
  }

  function loadAbout(st, hz, ab) {
    ab = ab || {};
    var box = $('setAbout');
    if (!box) return;
    var rows = [
      ['主进程', (ab.version || st.version || hz.version || '—') + '（pid ' + (ab.pid || st.host_pid || hz.pid || '?') + '）'],
      ['应用根', ab.app_root || '—'],
      ['平台目录', ab.core_dir || st.core_dir || '—'],
      ['库目录', (ab.vault || (st.notes || {}).dir || '—')
        + (ab.vault_from ? '（' + ab.vault_from + '）' : '')
        + (cfgNote(ab) ? '<br><span class="warn">config.yaml 里写的是：' + esc(ab.vault_config)
           + (ab.vault_config_ok ? '' : '（这个路径在这台机器上不存在，已按包内 core-notes 用）')
           + '</span>' : '')],
      ['界面地址', ab.console || st.console || '—'],
      ['界面装配', ab.ui_assets ? ('http://127.0.0.1:' + ab.ui_assets + '/console') : '—'],
      ['用户目录', ab.user_dir || '—'],
      ['窗口', '—'],
      ['窗口状态文件', '—']
    ];
    // ⚠️ 值里允许一段受控 HTML（只有"库目录"那行会带一句 config 的说明，内容都过了 esc）
    box.innerHTML = rows.map(function (r) {
      var v = String(r[1]);
      var safe = v.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                  .replace(/&lt;br&gt;/g, '<br>')
                  .replace(/&lt;span class="warn"&gt;/g, '<span class="warn">')
                  .replace(/&lt;\/span&gt;/g, '</span>');
      return '<div class="fact"><span class="fk">' + esc(r[0]) + '</span><span class="fv" data-k="' + esc(r[0]) + '">' + safe + '</span></div>';
    }).join('');
    // 壳那两行：读 data/shell-state.json（壳每做一次动作就重写它）
    fetch('/shell-state.json', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var set = function (k, v) {
          var el = box.querySelector('[data-k="' + k + '"]');
          if (el) el.textContent = v;
        };
        set('窗口', '窗口版' + (d.shell_version && d.shell_version !== '?' ? ' ' + d.shell_version : ''));
        set('窗口状态文件', d.state_file || '—');
      })
      .catch(function () {
        var set = function (k, v) {
          var el = box.querySelector('[data-k="' + k + '"]');
          if (el) el.textContent = v;
        };
        set('窗口', '网页版（浏览器标签）');
        set('窗口状态文件', '（窗口版才有）');
      });
  }

  whenReady(wire);
})();
</script>
<script src="/page-panels.js"></script>
</body>
</html>
`
