using System.Text.Json;

namespace Shuge.Desktop;

/// <summary>
/// 页面 → 壳的命令处理（2026-10-11，B 方案第③块"应用内桥"）。
///
/// 走 WebView2 的**消息通道**（`window.chrome.webview.postMessage` + `WebMessageReceived`），
/// 不走 `AddHostObjectToScript`：
/// 本机实测（.NET 10 + WebView2 1.0.3485，自包含单文件）那条路**注册不上** ——
/// `chrome.webview.hostObjects.sync` 里没有 shuge，取它报「找不到元素 (0x80070490)」，
/// 而 `AddHostObjectToScript` 既不抛异常也不生效（连错误都没得看）。
/// 消息通道没有 COM 那一层，页面里一行 postMessage 就通，且失败看得见。
///
/// 页面这么用：
/// <code>
///   chrome.webview.postMessage(JSON.stringify({ shuge: "zoomIn" }));
/// </code>
/// 或者用设置页里那个 `shugeAct(name)`（它会自己挑通道、并带消息兜底）。
/// </summary>
internal static class ShellBridge
{
    /// <summary>认这条命令吗（认就执行，返回 true）。</summary>
    public static bool Handle(string json, MainForm form)
    {
        string cmd, why = "";
        try
        {
            using var d = JsonDocument.Parse(json);
            if (d.RootElement.ValueKind != JsonValueKind.Object) return false;
            if (!d.RootElement.TryGetProperty("shuge", out var v)) return false;
            cmd = v.GetString() ?? "";
            if (d.RootElement.TryGetProperty("why", out var w)) why = w.GetString() ?? "";
        }
        catch { return false; }

        switch (cmd)
        {
            case "zoomIn": form.BridgeZoom(+0.1); return true;
            case "zoomOut": form.BridgeZoom(-0.1); return true;
            case "zoomReset": form.BridgeZoomReset(); return true;
            case "toggleFullscreen": form.BridgeToggleFullscreen(); return true;
            case "reload": form.BridgeReload(); return true;
            case "openDevTools": form.BridgeDevTools(); return true;
            case "openInBrowser": form.BridgeOpenInBrowser(); return true;
            case "openDataDir": form.BridgeOpenDataDir(); return true;
            case "openVaultDir": form.BridgeOpenVaultDir(); return true;
            case "openBin": form.BridgeOpenBin(); return true;
            // 页面把结果回来说一声：成功只记一笔；**失败要弹出来**（"点了没反应"是最难查的那种）
            case "binOpened": form.BridgeNote("回收站已打开：" + why); return true;
            case "binFailed":
                form.BridgeNote("回收站没打开：" + why);
                MessageBox.Show("回收站没打开：" + why,
                    "枢阁", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return true;
            default: return false;
        }
    }

}
