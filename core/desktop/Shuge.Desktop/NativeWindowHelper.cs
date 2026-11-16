using System.Diagnostics;
using System.Runtime.InteropServices;

namespace Shuge.Desktop;

/// <summary>Win32 小工具：把已有窗口叫到前面、无窗口起进程、以及"宿主在不在"的判定。</summary>
internal static class NativeWindowHelper
{
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] private static extern IntPtr FindWindow(string? lpClassName, string lpWindowName);
    [DllImport("user32.dll")] private static extern bool ReleaseCapture();
    [DllImport("user32.dll")] private static extern IntPtr SendMessage(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);

    private const int WM_NCLBUTTONDOWN = 0x00A1;
    private const int HTCAPTION = 2;
    private const int SW_RESTORE = 9;

    /// <summary>再点一次启动器时：找到已开的枢阁窗口，恢复并置前。</summary>
    public static void FocusExisting()
    {
        var h = FindWindow(null, ShellInfo.WindowTitle);
        if (h == IntPtr.Zero) return;
        ShowWindow(h, SW_RESTORE);
        SetForegroundWindow(h);
    }

    /// <summary>
    /// 把"拖动窗口"这件事交回系统：自画的标题栏点住空白处时调它。
    /// 收益是**免费拿到系统行为**：拖到屏幕边贴靠、双击最大化/还原、Win+方向键、Win11 贴靠布局。
    /// </summary>
    public static void SystemCaptionDrag(IntPtr hwnd)
    {
        ReleaseCapture();
        SendMessage(hwnd, WM_NCLBUTTONDOWN, (IntPtr)HTCAPTION, IntPtr.Zero);
    }

    /// <summary>不弹黑窗地起一个进程（GUI 子系统 + DETACHED）。</summary>
    public static void SpawnHidden(string exe, string args, string workDir)
    {
        var psi = new ProcessStartInfo(exe, args)
        {
            WorkingDirectory = workDir,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        Process.Start(psi)?.Dispose();
    }

    /// <summary>本机端口有没有人在听（判"宿主在不在"用，不依赖任何 API 语义）。</summary>
    public static bool PortListening(int port, int timeoutMs = 500)
    {
        try
        {
            using var c = new System.Net.Sockets.TcpClient();
            var t = c.ConnectAsync("127.0.0.1", port);
            return t.Wait(timeoutMs) && c.Connected;
        }
        catch { return false; }
    }
}
