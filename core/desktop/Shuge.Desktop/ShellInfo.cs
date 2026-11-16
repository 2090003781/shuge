using System.Text.Json;

namespace Shuge.Desktop;

/// <summary>
/// 这个壳的"环境事实"：平台装在哪、宿主 exe 在哪、界面地址是什么、窗口状态存哪。
///
/// 原则（跟平台其它地方一致）：**不写死路径** —— 全从本 exe 自己的位置推：
///   &lt;平台&gt;\bin\shuge-app.exe   → 平台根 = 上两层；宿主 = 同目录的 core-host.exe
/// </summary>
internal sealed class ShellInfo
{
    public const string WindowTitle = "枢阁";
    public const int ConsolePortFallback = 8790;

    public string SelfPath { get; }
    public string PlatformDir { get; }
    public string HostExe { get; }
    public string StateFile { get; }
    public string WebViewDataDir { get; }
    public int ConsolePort { get; }

    /// <summary>界面装配端口（主进程里那条"只发界面"的本机监听；0 = 没有）。</summary>
    public int LocalAssetsPort { get; }

    public string ConsoleUrl => $"http://127.0.0.1:{ConsolePort}/console";

    /// <summary>窗口图标：平台 \bin\shuge.ico（scripts/make_icon.py 生成；没有就退回系统默认）。</summary>
    public string IconFile => Path.Combine(PlatformDir, "bin", "shuge.ico");

    /// <summary>兼容旧引用（= ConsoleUrl）。</summary>
    public string Url => ConsoleUrl;

    private ShellInfo(string self, string platform, string host, int port, int localPort)
    {
        SelfPath = self;
        PlatformDir = platform;
        HostExe = host;
        ConsolePort = port;
        LocalAssetsPort = localPort;
        var appData = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Shuge");
        Directory.CreateDirectory(appData);
        // 自检用的备用壳可以换一个状态文件（`--state-suffix`）：不然会跟主人那个窗口**互相覆盖**
        // （2026-10-12 踩：自检预置了 tab=settings，被在跑的壳按自己的状态写回去了）。
        StateFile = Path.Combine(appData, "desktop-shell" + StateSuffix + ".json");
        // 自检用的备用壳要**自己的 WebView2 数据目录**：两个宿主共用一个目录时，
        // 第二个会卡在浏览器进程锁上（表现：CDP 调试口怎么都起不来）——2026-10-11 踩。
        WebViewDataDir = Path.Combine(appData, "WebView2" + ProfileSuffix);
    }

    public static ShellInfo Detect()
    {
        var self = Environment.ProcessPath ?? AppContext.BaseDirectory;
        var bin = Path.GetDirectoryName(self) ?? AppContext.BaseDirectory;
        var platform = Path.GetDirectoryName(bin) ?? bin;

        // 宿主（core-host.exe）与 data\ 的位置必须一起认。两种摆法都合法：
        //   （甲）就地运行：壳在 <平台>\bin\      → 平台 `<平台>`，宿主 `bin\core-host.exe`，数据 `<平台>\data`
        //   （乙）包根运行：壳在 <包>\启动枢阁-窗口版.exe（make_program_pack.py 就是这么摆的，辞玖双击的就是它）
        //        → 平台 = <包>\core，宿主 `bin\core-host.exe`，数据 `<包>\core\data`
        // 2026-10-14 接手分析修：原来只按「exe 上两层 = 平台」推导 → 包根那份推到 <包> 的上一级，
        //   宿主找不到（不自动拉宿主）、host.port/ui-assets.port 读不到、shell-state.json 写到包外
        //   → 表现为「关于」里窗口那两行永远是「—/网页版」，且双击包根入口起不来。
        // 判据改成**按特征文件找**，不再按层数猜：谁下面有 core-host.exe 或 data\ 谁就是平台根。
        var host = Path.Combine(bin, "core-host.exe");
        var dataDir = Path.Combine(platform, "data");
        if (!File.Exists(host))
        {
            foreach (var cand in new[]
                     {
                         Path.Combine(platform, "bin", "core-host.exe"),
                         Path.Combine(platform, "core", "bin", "core-host.exe"),
                         Path.Combine(platform, "core-host.exe"),
                     })
            {
                if (File.Exists(cand)) { host = cand; break; }
            }
        }
        if (!Directory.Exists(dataDir))
        {
            foreach (var cand in new[]
                     {
                         Path.Combine(platform, "core", "data"),
                         Path.Combine(bin, "data"),
                     })
            {
                if (Directory.Exists(cand)) { dataDir = cand; break; }
            }
        }
        // 平台根以**能找到的那个**为准（找不到就退回原来的推导，不要比原来更差）
        if (File.Exists(host))
        {
            var hostDir = Path.GetDirectoryName(host)!;             // …\bin（或平台根）
            var up = Path.GetDirectoryName(hostDir);                // …\bin 的上一级
            if (up is not null && Directory.Exists(Path.Combine(up, "data"))) platform = up;
            else if (Directory.Exists(Path.Combine(hostDir, "data"))) platform = hostDir;
        }

        var port = ForcedPort > 0 ? ForcedPort : ConsolePortFallback;
        var local = 0;
        try
        {
            var data = dataDir;
            var f = Path.Combine(data, "host.port");
            if (File.Exists(f) && ForcedPort <= 0)
            {
                var txt = File.ReadAllText(f).Trim();
                if (int.TryParse(txt, out var p) && p > 0 && p < 65536) port = p;
            }
            // 界面装配端口（2026-10-10）：主进程另开的那条"只发界面"的本机监听
            var lf = Path.Combine(data, "ui-assets.port");
            if (File.Exists(lf))
            {
                var txt = File.ReadAllText(lf).Trim();
                if (int.TryParse(txt, out var lp) && lp > 0 && lp < 65536) local = lp;
            }
        }
        catch { }

        return new ShellInfo(self, platform, host, port, local);
    }

    /// <summary>测试用：强制端口（`--port 8899`）——验"连不上服务器时长什么样"。</summary>
    public static int ForcedPort = 0;

    /// <summary>测试用：数据目录后缀（`--profile-suffix test`）——备用壳跟主人的窗口互不打扰。</summary>
    public static string ProfileSuffix = "";

    /// <summary>测试用：窗口状态文件后缀（`--state-suffix test`）——同上，别互相覆盖。</summary>
    public static string StateSuffix = "";

    // ── 窗口状态（大小 / 位置 / 最大化 / 缩放）─────────────────────
    public sealed class WindowState
    {
        public int X { get; set; } = int.MinValue;
        public int Y { get; set; } = int.MinValue;
        public int W { get; set; } = 1360;
        public int H { get; set; } = 900;
        public bool Maximized { get; set; }
        public double Zoom { get; set; } = 1.0;

    }

    public WindowState LoadState()
    {
        try
        {
            if (File.Exists(StateFile))
                return JsonSerializer.Deserialize<WindowState>(File.ReadAllText(StateFile)) ?? new WindowState();
        }
        catch { }
        return new WindowState();
    }

    public void SaveState(WindowState st)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(StateFile)!);
            File.WriteAllText(StateFile,
                JsonSerializer.Serialize(st, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch { }
    }
}
