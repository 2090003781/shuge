# 枢阁 · 桌面窗口（本地软件形态）

辞玖 2026-10-09：「把 web 换成本地软件形式，网页版还是受限看着有点难受」
并明确参照 Obsidian：**不要那种"软件里塞了一个浏览器"的感觉**。

## 一、这是什么，不是什么

| 是 | 不是 |
|---|---|
| 一个**原生窗口**（WinForms），有标题栏、任务栏图标、最大化、原生菜单、F11 全屏 | 不是把浏览器窗口改个标题 |
| 界面由系统里的 **Edge WebView2 内核**渲染（本机 154.x，Win10/11 自带） | 不是把 Edge/Chrome 嵌进软件（没有地址栏、后退键、标签页、收藏夹） |
| 三个服务照旧：宿主 8790 / 库 8848 / 内核 8795 | 不是重写界面：编辑器、PDF/DOCX 引擎、主题、自检**全部复用** |
| 双击即可用；宿主不在会**自己把它拉起来** | 不需要先开浏览器、不需要装 Electron 运行时 |

**"像软件"的实测凭据**（`python scripts\check_desktop_shell.py` 量的）：

- 顶层窗口类 = `WindowsForms10.Window…`（原生窗口），**不是** `Chrome_WidgetWin_1`（浏览器窗口类）；
- 窗口里的控件里**没有任何浏览器 UI**（地址栏 Omnibox / 工具栏 Toolbar / 后退键 / 自动补全，一个都没有）；
- 有 WebView2 的渲染面（`Chrome_RenderWidgetHostHWND`）+ `msedgewebview2.exe` 进程 → 界面真在画；
- 窗口标题就是「枢阁」，不是「xxx - Microsoft Edge」。

## 二、怎么编、怎么用

```powershell
# 编译（本机要 .NET 10 SDK；发布出来是自包含单文件，目标机器不需要装 SDK）
cd D:\core\desktop\Shuge.Desktop
dotnet publish -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true
# 产物：bin\Release\net10.0-windows\win-x64\publish\shuge-app.exe
# 摆到平台：copy 到 <平台>\bin\shuge-app.exe
```

用法：
- 双击 `shuge-app.exe`（或包根目录的 `启动枢阁-窗口版.exe`）；
- 宿主没在跑 → 它用 `-open-web` 把宿主拉起来，等 8790 通，再开窗口；
- **最左边一列**（辞玖 2026-10-11 定的形状）：三个页签 🏛 控制台 / 📚 库 / 🔧 内核
  （Ctrl+1/2/3），**最底下**一个 ⚙ 设置（刷新、缩放、全屏、在浏览器打开、开发者工具、
  打开平台 data / 库目录、关于）。**没有顶部菜单横条**，也没有自画的圆角
  —— 圆角交给 Windows（DWM），上一版拿 Region 自己裁，把标题栏那三个键裁花了；
- 颜色跟主题同色：读 `GET /api/ui/theme`，浅色走护眼浅黄（那一列 `--bar` = `#F7F0DE`），深色那套变量也备好了；
- 窗口大小、位置、缩放值**记在** `%LOCALAPPDATA%\Shuge\desktop-shell.json`，下次照旧。

自检（都能重跑）：
```
python scripts\check_rail.py       # 那一列的几何：宽 56 / 底色同主题 / 三个页签 + 最底下设置键 / 正文留呼吸位
python scripts\check_tabs.py       # 三个页签真能切：起一个带调试口的备用壳，用 CDP 问页面 + 真点一下按钮
python scripts\shot_window.py      # 把窗口（连标题栏）截下来自己看
python scripts\shot_rail_tabs.py   # 控制台 / 库 两张图，摆到屏幕上拍
```
测试口子：`--debug-port 9345`（给窗口里那个 Edge 内核开 DevTools 调试口）、
`--mutex-suffix test`（单实例锁换名，可以跟主人的窗口同时开着跑自检）、
`--port 8899`（故意指没人听的端口）。

## 三、已知边界（如实）

1. **界面本体仍是"网页"**：由 8790（或主进程那条"只发界面"的本机监听）服务端渲染；
   库/内核**已经不是 iframe**（`web/page-panels.js` 把那一页的 HTML 装配进来），路由走
   `history`（不是 `#hash`）。剩"网页感"的地方：数据仍走 HTTP（不是应用内桥）。
2. WebView2 运行时是**系统组件**：全新机器上若没有，窗口会弹一句提示 + 一条微软官方下载链接（免费）。
3. 图标目前用系统默认；`<平台>\bin\shuge.ico` 放一个就会自动用上。

## 四、下一步（照 Obsidian 那条路，等辞玖点头）

| 步 | 做什么 | 为什么 |
|---|---|---|
| 1 | ~~库与内核不再用 iframe 套~~ **已完成**：改成同页装配的面板 + 壳上那一列页签 | 去掉"嵌套文档"的浏览器感 |
| 2 | ~~去掉 `#hash` 路由~~ **已完成**：改 `history` 路由，页签在壳上切（不整页刷新） | 去掉"每次都是加载一个页面" |
| 3 | ~~界面资源随软件走（本地装配）~~ **已完成**：`data/ui-assets.port` 那条本机监听从磁盘直接发界面 | 断网、端口抖动不再"转圈" |
| 4 | 命令面板（Ctrl+P）+ 快捷键体系（Ctrl+, 设置） | Obsidian 的手感 |
| 5 | 导入/导出走系统原生对话框 | "网页选择框"换掉 |

工作量：第 1–3 步是大头（界面改架构，**服务端与引擎一行不动**），4–5 步是小活。
