using System.Runtime.InteropServices;

namespace Shuge.Desktop;

internal static class Program
{
    [DllImport("shcore.dll")] private static extern int SetProcessDpiAwareness(int value);
    [DllImport("user32.dll")] private static extern bool SetProcessDpiAwarenessContext(IntPtr ctx);

    [STAThread]
    private static void Main(string[] args)
    {
        // DPI 必须**在**建窗口之前定下来，本机是 125%（DPI=120）。
        // ⚠️ 2026-10-11 踩了一大坑：只用 ApplicationConfiguration.Initialize() 时，
        //    这个进程对系统并不是 PerMonitorV2 —— 于是 WinForms 认为"客户区 1360x900"，
        //    而物理窗口只有 1088x720（少了 0.8 倍），我在逻辑坐标里算的那三个窗口键
        //    就被排到窗口外面去了（肉眼看不见、鼠标也够不着）。
        //    这里显式设一遍（跟 csproj 的 ApplicationHighDpiMode 一致）。
        try { SetProcessDpiAwarenessContext(new IntPtr(-4)); }   // PER_MONITOR_AWARE_V2
        catch { try { SetProcessDpiAwareness(2); } catch { } }    // 退路：PROCESS_PER_MONITOR_DPI_AWARE

        ApplicationConfiguration.Initialize();   // 含字体与 WinForms 初始化

        // 单实例：再点一次启动器时，把已有窗口叫到前面，而不是开第二个
        var mutexName = @"Local\shuge-desktop-shell";
        for (var i = 0; i < args.Length - 1; i++)
            if (args[i] == "--mutex-suffix") mutexName = @"Local\shuge-desktop-shell-" + args[i + 1];
        using var mutex = new Mutex(true, mutexName, out var isFirst);
        if (!isFirst) { NativeWindowHelper.FocusExisting(); return; }

        // 测试口子：--profile-suffix test → 备用壳用自己的 WebView2 数据目录（不跟主人的窗口抢）
        for (var i = 0; i < args.Length - 1; i++)
            if (args[i] == "--profile-suffix") ShellInfo.ProfileSuffix = "-" + args[i + 1];

        // 测试口子：--state-suffix test → 备用壳用自己的窗口状态文件（免得跟主人的窗口互相覆盖）
        for (var i = 0; i < args.Length - 1; i++)
            if (args[i] == "--state-suffix") ShellInfo.StateSuffix = "-" + args[i + 1];

        // 测试口子：--debug-port 9345 → 给窗口里那个 Edge 内核开调试口（自检用 DevTools 协议问页面）
        for (var i = 0; i < args.Length - 1; i++)
            if (args[i] == "--debug-port" && int.TryParse(args[i + 1], out var dbg) && dbg > 0)
                MainForm.DebugPort = dbg;

        // 测试口子：--port 8899（故意指一个没人听的端口，用来验连不上时界面长什么样）
        for (var i = 0; i < args.Length - 1; i++)
            if (args[i] == "--port" && int.TryParse(args[i + 1], out var portNum) && portNum > 0) ShellInfo.ForcedPort = portNum;

        Application.Run(new MainForm(args));
    }
}
