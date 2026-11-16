using System.Runtime.InteropServices;

namespace Shuge.Desktop;

/// <summary>
/// 页面 ↔ 壳的那道桥（2026-10-11，B 方案第③块"应用内桥"的第一步）。
///
/// 页面里这么用（设置页就是这么写的）：
/// <code>
///   var b = window.chrome.webview.hostObjects.sync.shuge;   // 同步代理
///   b.zoomIn(); b.toggleFullscreen(); b.openDataDir(); …
/// </code>
/// 为什么要有它：缩放 / 全屏 / 开发者工具 / 打开目录 / 在浏览器打开 —— 这些**只有窗口能做**，
/// 网页里没有对应动作。没有这道桥，设置页就只能弹壳的原生菜单（上一版就是那样）。
///
/// 注意：它是"页面能叫窗口做事"的单向通道，**不是**给页面读窗口内部状态的；
/// 页面上要的窗口信息（有没有桥、当前主题）本来就拿得到。
/// </summary>
// ⚠️ 类本身保持 internal（这个工程是 WinExe，不需要对外暴露类型），
//    接口类型用 AutoDispatch（晚绑定）—— AutoDual 会把 MainForm 这种内部类型也塞进
//    类型库，编译报 CS0051「可访问性不一致」。
[ComVisible(true)]
[ClassInterface(ClassInterfaceType.AutoDispatch)]
internal sealed class ShugeHost
{
    private readonly MainForm _form;

    internal ShugeHost(MainForm form) => _form = form;

    public void zoomIn() => _form.BridgeZoom(+0.1);
    public void zoomOut() => _form.BridgeZoom(-0.1);
    public void zoomReset() => _form.BridgeZoomReset();
    public void toggleFullscreen() => _form.BridgeToggleFullscreen();
    public void reload() => _form.BridgeReload();
    public void openDevTools() => _form.BridgeDevTools();
    public void openInBrowser() => _form.BridgeOpenInBrowser();
    public void openDataDir() => _form.BridgeOpenDataDir();
    public void openVaultDir() => _form.BridgeOpenVaultDir();
}
