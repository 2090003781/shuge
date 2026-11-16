using System.Drawing.Drawing2D;
using System.Net.Http;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace Shuge.Desktop;

/// <summary>
/// 枢阁的原生窗口（本地软件形态）。
///
/// 辞玖 2026-10-10 19:07 定下的形状：
///   · **不要顶部黑条** —— 原来那条"☰ 枢阁"菜单横条整个去掉；
///   · 控制台 / 库 / 内核 → **最左侧一列页签**（窄轨；图标自己画，不依赖 emoji 字体）；
///   · **最底下放设置键**（⚙），原来"工具"里的东西收进它的菜单；
///   · 颜色**跟随主题**（读主进程 /api/ui/theme），当前统一走护眼浅黄。
///
/// 仍然管这些：真窗口/记忆/缩放/全屏/开发者工具/外部链接丢系统浏览器/启动器语义。
/// </summary>
internal sealed class MainForm : Form
{
    private readonly ShellInfo _info;
    private readonly ShellInfo.WindowState _state;

    private WebView2? _view;
    private Panel? _splash;
    private Label? _splashText;
    private Panel? _rail;
    private NavButton? _gear;
    private TitleBar? _title;
    internal const int RailWidthLogical = 56;

    /// <summary>
    /// 圆角由 Windows 自己画（Win11 默认就是圆的）。这里只是**明确要求**一下，
    /// 绝不去裁窗口区域 —— 上一版拿 Region 裁圆角，把标题栏那三个键一起裁花了。
    /// </summary>
    private void AskSystemRoundedCorners()
    {
        try
        {
            const int DWMWA_WINDOW_CORNER_PREFERENCE = 33;
            const int DWMWCP_ROUND = 2;
            var v = DWMWCP_ROUND;
            DwmSetWindowAttribute(Handle, DWMWA_WINDOW_CORNER_PREFERENCE, ref v, sizeof(int));
        }
        catch { }
    }

    [System.Runtime.InteropServices.DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);

    /// <summary>把一个逻辑字号按当前窗口缩放放大（TextRenderer 自己不会替我们缩）。</summary>
    internal Font ScaledFont(Font baseFont)
    {
        var s = DeviceDpi / 96f;
        return new Font(baseFont.FontFamily, baseFont.Size * s, baseFont.Style, GraphicsUnit.Point);
    }

    /// <summary>标题栏上的"当前位置"（控制台 / 库 / 内核 / 设置）。</summary>
    public string TabTitle { get; private set; } = "控制台";

    /// <summary>标题栏左边那个小图标（"枢"）；读不到就是 null（标题栏只剩文字）。</summary>
    public Image? TitleIcon { get; private set; }

    /// <summary>标题栏用的两个字体（品牌 / 当前位置）。</summary>
    public Font TitleFont { get; } = new("Microsoft YaHei UI", 10.5f, FontStyle.Bold);
    public Font TitleSubFont { get; } = new("Microsoft YaHei UI", 9.5f);
    private readonly Dictionary<string, NavButton> _navBtns = new();
    private string _tab = "console";
    private string _themeName = "light";
    private string _workspaceUrl = "";

    /// <summary>自检口子：给窗口里的 Edge 内核开 DevTools 调试口（0 = 不开）。</summary>
    public static int DebugPort = 0;

    /// <summary>桥的近况（空 = 正常）。写进 data/shell-state.json，自检脚本读它 —— 失败不静默。</summary>
    public static string BridgeError = "未尝试";

    /// <summary>把"桥通不通 + 当前缩放"落到 data/shell-state.json：自检与页面都能对得上。</summary>
    internal void BridgeNote(string note)
    {
        BridgeError = note;
        try
        {
            var dir = Path.Combine(_info.PlatformDir, "data");
            Directory.CreateDirectory(dir);
            var js = System.Text.Json.JsonSerializer.Serialize(new
            {
                at = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"),
                bridge = note.Length == 0 ? "ok" : ("err: " + note),
                channel = "webmessage",
                zoom = Math.Round(_view?.ZoomFactor ?? 0, 3),
                fullscreen = _fullscreen,
                shell_version = typeof(MainForm).Assembly.GetName().Version?.ToString() ?? "?",
                workspace = _workspaceUrl,
                state_file = _info.StateFile,
            }, new System.Text.Json.JsonSerializerOptions { WriteIndented = true });
            File.WriteAllText(Path.Combine(dir, "shell-state" + ShellInfo.StateSuffix + ".json"), js);
        }
        catch { }
    }

    private bool _fullscreen;
    private FormWindowState _beforeFullscreen = FormWindowState.Normal;
    private FormBorderStyle _beforeBorder = FormBorderStyle.Sizable;
    private Rectangle _beforeBounds;

    // ── 主题色：读共享的那一份（ShellPalette），浅/深两套都在那边 ──
    public ShellPalette Palette = new();
    // 这些给 TitleBar 也读，所以是 internal 可见（同一个程序集里）
    internal Color RailBg => Palette.Bg;
    internal Color RailLine => Palette.Line;
    internal Color RailFg => Palette.Fg;
    internal Color RailDim => Palette.Dim;
    internal Color RailHover => Palette.Hover;
    internal Color RailActive => Palette.Active;
    internal Color RailActiveFg => Palette.ActiveFg;
    internal Color RailBar => Palette.Bg;
    internal Color RailMenuBg => Palette.MenuBg;
    private const int RailW = 56;

    public MainForm(string[] args)
    {
        _info = ShellInfo.Detect();
        _state = _info.LoadState();
        _themeName = ReadTheme();
        SetPalette(_themeName);

        Text = ShellInfo.WindowTitle;
        MinimumSize = new Size(760, 520);
        BackColor = RailBar;          // 窗口底色 = 那一列的同色，不露黑边
        Icon = TryLoadIcon();
        // 自己画标题栏（辞玖 2026-10-11：仿 Obsidian 把最上边那条用起来）：
        // 系统那条又厚又空的白条换成 40px 的一条（品牌 + 当前位置 + 三个窗口键），
        // 边框/拉伸由本类的 WM_NCHITTEST 管，拖动交给系统（双击最大化、贴靠照旧）。
        FormBorderStyle = FormBorderStyle.None;
        // ⚠️ 状态里存的是**整窗**尺寸（物理像素），必须用 Bounds 恢复，**不能**用 ClientSize：
        //    本机 DPI=120（125%），ClientSize 会被 AutoScaleMode 再乘一次缩放 → 越开越大 / 越开越小。
        // ⚠️ 状态里存的是**物理像素**，而 WinForms 的 Bounds 是**逻辑像素** ——
        //    本机 DPI=125% 时直接把物理值塞进 Bounds，窗口就会被再放大 1.25 倍
        //    （实测：存 1194x798 → 客户区 1493x998，标题栏因此比窗口还宽、右下角还超屏）。
        //    所以这里按缩放换算一次；保存时再换回去。
        var sc = DeviceDpi / 96f;
        Bounds = new Rectangle(
            _state.X == int.MinValue ? 0 : _state.X,
            _state.Y == int.MinValue ? 0 : _state.Y,
            Math.Max((int)Math.Round(_state.W / sc), 760),
            Math.Max((int)Math.Round(_state.H / sc), 520));
        var onScreen = _state.X != int.MinValue &&
                       Screen.AllScreens.Any(sc => sc.WorkingArea.IntersectsWith(Bounds));
        if (!onScreen) { StartPosition = FormStartPosition.CenterScreen; Bounds = new Rectangle(0, 0, 1360, 900); }
        LayoutChrome();
        if (_state.Maximized) WindowState = FormWindowState.Maximized;
        AskSystemRoundedCorners();

        BuildRail();
        BuildTitleBar();
        BuildSplash();
        Shown += async (_, _) => await BootAsync();
        FormClosing += OnFormClosing;
        // 尺寸/位置**边改边记**（被 taskkill 结束的进程不走 FormClosing，那样记忆永远不生效）。
        ResizeEnd += (_, _) => SaveStateSoon();
        Move += (_, _) => SaveStateSoon();
        Resize += (_, _) => SaveStateSoon();
        KeyPreview = true;
        KeyDown += OnKeyDown;
        // 焦点在网页里时，WebView2 会把按键自己吃掉（KeyPreview 收不到）——
        // 所以再挂一道**消息过滤器**，在 WebView 拿到 WM_KEYDOWN 之前先把热键截住
        // （2026-10-12 虚拟机实测：不挂这个，Ctrl+4 切设置毫无反应）。
        Application.AddMessageFilter(new HotkeyFilter(this));
    }

    /// <summary>最上边那一条（品牌 + 当前位置 + 最小化/最大化/关闭）。</summary>
    private void BuildTitleBar()
    {
        _title = new TitleBar(this);
        Controls.Add(_title);
        Controls.SetChildIndex(_title, 0);       // 最前：三个窗口键要压在最上面
        LayoutChrome();
    }

    /// <summary>把"我在哪一格"写到标题栏上（切页签时调）。</summary>
    private void ShowTabTitle(string tab)
    {
        TabTitle = tab switch
        {
            "notes" => "库",
            "core" => "内核",
            "settings" => "设置",
            _ => "控制台",
        };
        _title?.Invalidate();
    }

    // ── 无边框窗口该有的那几件事：拉伸边、三个窗口键、最大化贴工作区 ──
    protected override void WndProc(ref Message m)
    {
        const int WM_NCHITTEST = 0x0084, WM_NCLBUTTONDOWN = 0x00A1, WM_GETMINMAXINFO = 0x0024;
        const int HTCLIENT = 1, HTMINBUTTON = 8, HTMAXBUTTON = 9, HTCLOSE = 20;
        const int HTLEFT = 10, HTRIGHT = 11, HTTOP = 12, HTTOPLEFT = 13, HTTOPRIGHT = 14,
                  HTBOTTOM = 15, HTBOTTOMLEFT = 16, HTBOTTOMRIGHT = 17;

        if (m.Msg == WM_NCHITTEST)
        {
            base.WndProc(ref m);
            var lp = m.LParam.ToInt64();
            var p = PointToClient(new Point((short)(lp & 0xFFFF), (short)((lp >> 16) & 0xFFFF)));
            const int g = 7;                      // 拉伸热区宽度
            var max = WindowState == FormWindowState.Maximized;

            // 标题栏那三个键：告诉系统"这里是最大化键"，于是 Win11 的贴靠布局照样弹
            if (!max && _title is not null && p.Y < _title.Height && p.X > _title.Width - 46 * 3)
            {
                var i = (p.X - (_title.Width - 46 * 3)) / 46;
                _title.SetHot(p);
                m.Result = (IntPtr)(i == 0 ? HTMINBUTTON : i == 1 ? HTMAXBUTTON : HTCLOSE);
                return;
            }
            _title?.SetHot(null);

            if (!max)
            {
                if (p.Y < g && p.X < g) { m.Result = (IntPtr)HTTOPLEFT; return; }
                if (p.Y < g && p.X > ClientSize.Width - g) { m.Result = (IntPtr)HTTOPRIGHT; return; }
                if (p.Y > ClientSize.Height - g && p.X < g) { m.Result = (IntPtr)HTBOTTOMLEFT; return; }
                if (p.Y > ClientSize.Height - g && p.X > ClientSize.Width - g) { m.Result = (IntPtr)HTBOTTOMRIGHT; return; }
                if (p.Y < g) { m.Result = (IntPtr)HTTOP; return; }
                if (p.Y > ClientSize.Height - g) { m.Result = (IntPtr)HTBOTTOM; return; }
                if (p.X < g) { m.Result = (IntPtr)HTLEFT; return; }
                if (p.X > ClientSize.Width - g) { m.Result = (IntPtr)HTRIGHT; return; }
            }
            _ = HTCLIENT;
            return;
        }

        if (m.Msg == WM_NCLBUTTONDOWN)
        {
            // 那三个键由系统报"按下哪个"（HTMINBUTTON/HTMAXBUTTON/HTCLOSE 得自己接）
            var hit = m.WParam.ToInt32();
            if (hit == HTMINBUTTON) { CaptionMinimize(); return; }
            if (hit == HTMAXBUTTON) { CaptionMaximize(); return; }
            if (hit == HTCLOSE) { Close(); return; }
        }

        if (m.Msg == WM_GETMINMAXINFO && WindowState == FormWindowState.Maximized)
        {
            // 无边框窗口最大化的默认尺寸会盖住任务栏，钳到工作区
            base.WndProc(ref m);
            var mmi = System.Runtime.InteropServices.Marshal.PtrToStructure<MINMAXINFO>(m.LParam);
            var wa = Screen.FromHandle(Handle).WorkingArea;
            var mon = Screen.FromHandle(Handle).Bounds;
            mmi.ptMaxPosition.X = wa.X - mon.X;
            mmi.ptMaxPosition.Y = wa.Y - mon.Y;
            mmi.ptMaxSize.X = wa.Width;
            mmi.ptMaxSize.Y = wa.Height;
            System.Runtime.InteropServices.Marshal.StructureToPtr(mmi, m.LParam, false);
            return;
        }

        base.WndProc(ref m);
    }

    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    private struct POINTAPI { public int X; public int Y; }

    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    private struct MINMAXINFO
    {
        public POINTAPI ptReserved, ptMaxSize, ptMaxPosition, ptMinTrackSize, ptMaxTrackSize;
    }

    /// <summary>标题栏上那颗"最小化"。</summary>
    public void CaptionMinimize() => WindowState = FormWindowState.Minimized;

    /// <summary>标题栏上那颗"最大化/还原"。</summary>
    public void CaptionMaximize()
    {
        WindowState = WindowState == FormWindowState.Maximized
            ? FormWindowState.Normal
            : FormWindowState.Maximized;
        _title?.Refresh();
    }

    /// <summary>从主进程读当前主题（拿不到就按护眼浅色）。</summary>
    private string ReadTheme()
    {
        try
        {
            using var c = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
            var t = c.GetStringAsync($"http://127.0.0.1:{_info.ConsolePort}/api/ui/theme").GetAwaiter().GetResult();
            using var d = JsonDocument.Parse(t);
            if (d.RootElement.TryGetProperty("theme", out var v)) return v.GetString() ?? "light";
        }
        catch { }
        return "light";
    }

    /// <summary>主题色落地（浅色=护眼浅黄；深色那套也在 ShellPalette 里）。</summary>
    private void SetPalette(string theme) => Palette.Use(theme);

    // ── 最左侧那一列：四个页签（控制台/库/内核/设置） + 最底下那颗设置键 ──
    //
    // ⚠️ 位置一律由 LayoutRailItems() 按**物理像素**排，不用 Dock/Flow：
    //    本机 DPI=125%，WinForms 会把 Fixed 尺寸的控件再按缩放乘一次，Dock 也跟着乱
    //    （实测那一列被排到 45px、标题栏被撑到 1493 宽而窗口只有 1194 —— 2026-10-11）。
    private void BuildRail()
    {
        _rail = new Panel { BackColor = RailBg };
        // 右侧一条细分隔线（现代软件那种 1px 边界）
        _rail.Paint += (_, e) =>
        {
            using var pen = new Pen(RailLine);
            e.Graphics.DrawLine(pen, _rail.Width - 1, 0, _rail.Width - 1, _rail.Height);
        };

        NavButton Nav(string glyph, string tip, string tab)
        {
            var b = new NavButton(glyph, this) { TabStop = false };
            new ToolTip().SetToolTip(b, tip);
            b.Click += (_, _) => Go(tab);
            _navBtns[tab] = b;
            _rail.Controls.Add(b);
            return b;
        }

        // 图标：控制台=房子、库=书、**内核=齿轮**（辞玖 2026-10-11：「内核图标换成一个能表示
        // 平台或者核心的」—— 齿轮就是这个意思）、设置=带齿的轮子（跟内核区分开）
        Nav("\U0001F3DB", "控制台（主进程与整体生命）  Ctrl+1", "console");
        Nav("\U0001F4DA", "库（笔记）  Ctrl+2", "notes");
        Nav("\U0001F9E9", "内核（平台核心：总线 / 账本 / 插件）  Ctrl+3", "core");
        // ⚠️ 齿轮**只表示设置**（辞玖 2026-10-11 12:22：「齿轮图标还是用来表示设置，这个别动」）
        Nav("\u2699\uFE0F", "设置  Ctrl+4", "settings");

        // 最底下那一颗：**⋯ 更多**（辞玖 2026-10-12：「主程序界面的这个位置就可以换成一个
        // 承载其他功能的图标」）—— 回收站、在浏览器打开、开发者工具、打开目录、设置都收进它。
        // 为什么不再直接放回收站：回收站属于"库"那一页（那一页本来就有入口），
        // 放在这里是"点了只跳页"，不如做成一格装多个功能的入口。
        var more = new NavButton("\u22EF", this) { TabStop = false };
        new ToolTip().SetToolTip(more, "更多（回收站 / 在浏览器打开 / 开发者工具 / 打开目录 / 设置）");
        more.Click += (_, _) => ShowMore(more);
        _gear = more;
        _rail.Controls.Add(more);

        Controls.Add(_rail);
        // WinForms 里 child index 0 = **最前**（最后加进来的默认就在 0）。
        // 那一列先加（自动在最后 = 最底），页面后加再钉到 0（最前）。
        Controls.SetChildIndex(_rail, Controls.Count - 1);
        HighlightNav("console");
    }

    private void HighlightNav(string tab)
    {
        _tab = tab;
        foreach (var (k, b) in _navBtns)
        {
            b.Active = k == tab;
            b.Invalidate();
        }
    }

    /// <summary>
    /// 全局热键过滤器：壳的热键在**网页有焦点时**也要生效。
    /// WinForms 的 KeyPreview 只对"窗体自己收到"的按键有效，而 WebView2 是独立 HWND、会先吃掉按键。
    /// </summary>
    private sealed class HotkeyFilter : IMessageFilter
    {
        private const int WM_KEYDOWN = 0x0100, WM_SYSKEYDOWN = 0x0104;
        private readonly MainForm _f;

        public HotkeyFilter(MainForm f) => _f = f;

        public bool PreFilterMessage(ref Message m)
        {
            if (m.Msg != WM_KEYDOWN && m.Msg != WM_SYSKEYDOWN) return false;
            if (!_f.IsHandleCreated || Form.ActiveForm != _f) return false;

            var key = (Keys)m.WParam | Control.ModifierKeys;
            switch (key)
            {
                case Keys.F11: _f.ToggleFullscreen(); return true;
                case Keys.F12: _f.ToggleDevTools(); return true;
                case Keys.F5: _f.BridgeReload(); return true;
                case Keys.Control | Keys.D1: _f.Go("console"); return true;
                case Keys.Control | Keys.D2: _f.Go("notes"); return true;
                case Keys.Control | Keys.D3: _f.Go("core"); return true;
                case Keys.Control | Keys.D4: _f.Go("settings"); return true;
                case Keys.Control | Keys.D0: _f.SetZoom(1.0); return true;
                case Keys.Control | Keys.Oemplus:
                case Keys.Control | Keys.Add: _f.Zoom(+0.1); return true;
                case Keys.Control | Keys.OemMinus:
                case Keys.Control | Keys.Subtract: _f.Zoom(-0.1); return true;
                case Keys.Control | Keys.Shift | Keys.B: _f.OpenInBrowser(); return true;
            }
            return false;
        }
    }

    /// <summary>
    /// 一个页签（或最底下那个设置键）：圆角底 + 一个 emoji 字形。
    /// 图标就用网页版页签上原来那三个（辞玖 2026-10-11：「图标用之前的」）。
    /// </summary>
    private sealed class NavButton : Control
    {
        private readonly string _glyph;
        private readonly MainForm _owner;
        private bool _hover;
        private bool _active;

        // 纯代码画出来的控件，不走设计器序列化
        [System.ComponentModel.DesignerSerializationVisibility(
            System.ComponentModel.DesignerSerializationVisibility.Hidden)]
        [System.ComponentModel.Browsable(false)]
        public bool Active
        {
            get => _active;
            set { if (_active != value) { _active = value; Invalidate(); } }
        }

        public NavButton(string glyph, MainForm owner)
        {
            _glyph = glyph;
            _owner = owner;
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.UserPaint |
                     ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
            Cursor = Cursors.Hand;
            Font = GlyphFont(18f);
        }

        /// <summary>emoji 走 Segoe UI Emoji（雅黑自己画不出这些字形）；没有就退回雅黑。</summary>
        private static Font GlyphFont(float size)
        {
            try { return new Font("Segoe UI Emoji", size); }
            catch { return new Font("Microsoft YaHei UI", size); }
        }

        /// <summary>几何由 LayoutRailItems 按物理像素算，别让父窗体再缩一次。</summary>
        protected override void ScaleControl(SizeF factor, BoundsSpecified specified) { }

        protected override void OnMouseEnter(EventArgs e) { _hover = true; Invalidate(); base.OnMouseEnter(e); }
        protected override void OnMouseLeave(EventArgs e) { _hover = false; Invalidate(); base.OnMouseLeave(e); }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.AntiAliasGridFit;

            if (_active || _hover)
            {
                var r = new Rectangle(0, 0, Width - 1, Height - 1);
                using var path = Round(r, 9);
                using var br = new SolidBrush(_active ? _owner.RailActive : _owner.RailHover);
                g.FillPath(br, path);
            }

            var ink = _active ? _owner.RailActiveFg : _owner.RailFg;
            var sz = TextRenderer.MeasureText(g, _glyph, Font, new Size(999, 999), TextFormatFlags.NoPadding);
            TextRenderer.DrawText(g, _glyph, Font,
                new Point((Width - sz.Width) / 2, (Height - sz.Height) / 2), ink,
                TextFormatFlags.NoPadding);
        }

        private static GraphicsPath Round(Rectangle r, int radius)
        {
            var p = new GraphicsPath();
            var d = radius * 2;
            p.AddArc(r.X, r.Y, d, d, 180, 90);
            p.AddArc(r.Right - d, r.Y, d, d, 270, 90);
            p.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
            p.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
            p.CloseFigure();
            return p;
        }
    }

    /// <summary>
    /// 最底下那颗 ⋯ 的菜单：装"其他功能"。
    /// 回收站那一项走页面里的 window.__shugeOpenBin（它会自己切到能开回收站的那一页并等装配好）。
    /// </summary>
    private void ShowMore(Control anchor)
    {
        var m = NewMenu();
        void Item(string text, Action act)
        {
            var it = new ToolStripMenuItem(text);
            it.Click += (_, _) => act();
            m.Items.Add(it);
        }
        Item("🗑  回收站", OpenBin);
        m.Items.Add(new ToolStripSeparator());
        Item("设置", () => Go("settings"));
        Item("在浏览器打开", OpenInBrowser);
        Item("开发者工具（F12）", ToggleDevTools);
        m.Items.Add(new ToolStripSeparator());
        Item("打开平台 data 目录", BridgeOpenDataDir);
        Item("打开库目录", BridgeOpenVaultDir);
        m.Show(anchor, new Point(anchor.Width - 4, 0));
    }

    /// <summary>一份统一配色的原生菜单（设置/更多两处共用）。</summary>
    private ContextMenuStrip NewMenu()
    {
        var m = new ContextMenuStrip
        {
            BackColor = RailMenuBg,
            ForeColor = RailFg,
            Renderer = new ToolStripProfessionalRenderer(new RailColors(this)) { RoundedEdges = false },
            ShowImageMargin = false,
        };
        return m;
    }

    /// <summary>设置（原"工具"那些）：在 ⚙ 旁边弹一份菜单。</summary>
    private void ShowSettings(Control anchor)
    {
        var m = NewMenu();
        void Item(string text, Action act)
        {
            var it = new ToolStripMenuItem(text);
            it.Click += (_, _) => act();
            m.Items.Add(it);
        }
        Item("刷新界面（F5）", () => _view?.Reload());
        m.Items.Add(new ToolStripSeparator());
        Item("放大（Ctrl+滚轮）", () => Zoom(+0.1));
        Item("缩小", () => Zoom(-0.1));
        Item("实际大小（Ctrl+0）", () => SetZoom(1.0));
        Item("全屏（F11）", ToggleFullscreen);
        m.Items.Add(new ToolStripSeparator());
        Item("在浏览器打开（Ctrl+Shift+B）", OpenInBrowser);
        Item("开发者工具（F12）", ToggleDevTools);
        m.Items.Add(new ToolStripSeparator());
        Item("打开平台 data 目录", () => NativeWindowHelper.SpawnHidden(
            "explorer.exe", Path.Combine(_info.PlatformDir, "data"), _info.PlatformDir));
        Item("打开库目录", () =>
        {
            var vault = ReadVaultPath();
            NativeWindowHelper.SpawnHidden("explorer.exe",
                Directory.Exists(vault) ? vault : _info.PlatformDir, _info.PlatformDir);
        });
        m.Show(anchor, new Point(anchor.Width - 4, 0));
    }

    private sealed class RailColors : ProfessionalColorTable
    {
        private readonly MainForm _o;
        public RailColors(MainForm o) => _o = o;
        public override Color MenuItemSelected => _o.RailHover;
        public override Color MenuItemBorder => _o.RailActiveFg;
        public override Color ToolStripDropDownBackground => _o.RailMenuBg;
        public override Color MenuBorder => _o.RailLine;
        public override Color ImageMarginGradientBegin => _o.RailMenuBg;
        public override Color ImageMarginGradientMiddle => _o.RailMenuBg;
        public override Color ImageMarginGradientEnd => _o.RailMenuBg;
    }

    /// <summary>从 config.yaml 抠出 resources.vault.path（只为"打开库目录"；抠不到就不开）。</summary>
    private string ReadVaultPath()
    {
        try
        {
            var cfg = Path.Combine(_info.PlatformDir, "config.yaml");
            if (!File.Exists(cfg)) return "";
            var lines = File.ReadAllLines(cfg);
            for (var i = 0; i < lines.Length; i++)
            {
                if (!lines[i].TrimStart().StartsWith("vault:", StringComparison.Ordinal)) continue;
                for (var j = i; j < Math.Min(i + 4, lines.Length); j++)
                {
                    var t = lines[j].Trim();
                    if (t.StartsWith("path:", StringComparison.Ordinal))
                        return t[5..].Trim().Trim('"', '\'').Replace('/', Path.DirectorySeparatorChar);
                }
            }
        }
        catch { }
        return "";
    }

    // ── 开场 ───────────────────────────────────────────────────────
    private void BuildSplash()
    {
        _splash = new Panel { Dock = DockStyle.Fill, BackColor = RailBar };
        var title = new Label
        {
            Text = "🏛  枢阁",
            ForeColor = RailActiveFg,
            Font = new Font("Microsoft YaHei UI", 22f, FontStyle.Bold),
            AutoSize = true,
        };
        _splashText = new Label
        {
            Text = "正在启动…",
            ForeColor = RailDim,
            Font = new Font("Microsoft YaHei UI", 10f),
            AutoSize = true,
        };
        _splash.Controls.Add(title);
        _splash.Controls.Add(_splashText);
        _splash.Resize += (_, _) =>
        {
            title.Location = new Point((_splash.Width - title.Width) / 2, _splash.Height / 2 - 40);
            _splashText.Location = new Point((_splash.Width - _splashText.Width) / 2, _splash.Height / 2 + 10);
        };
        Controls.Add(_splash);
        _splash.BringToFront();
    }

    private async Task BootAsync()
    {
        SetSplash("正在检查枢阁主程序…");
        if (!NativeWindowHelper.PortListening(_info.ConsolePort))
        {
            if (File.Exists(_info.HostExe))
            {
                SetSplash("正在启动枢阁主程序…");
                // 用 --force-start，**不用 -open-web**（2026-10-14 接手分析）：
                //   · -open-web 的语义含「在系统浏览器里打开控制台」（main.go:158 openBrowser）——
                //     而**壳自己就是浏览器**，于是双击窗口版会额外弹一个浏览器标签页。
                //   · 但不能简单删掉参数：-open-web 还兼职做了 main.go:132 的 setHostPaused(false)，
                //     少了它，主人用「⏻ 关闭枢阁」关过一次后壳就再也拉不起来。
                //   · --force-start 正是「打开枢阁」的语义（main.go:187-188 清暂停与熔断、
                //     :216-218 保活启停台、:221-222 拉库、:231 拉内核），只差最后不开浏览器 —— 正合适。
                NativeWindowHelper.SpawnHidden(_info.HostExe, "--force-start", _info.PlatformDir);
            }
            else
            {
                SetSplash($"找不到主程序：{_info.HostExe}\n（core\\bin\\core-host.exe 应当和我在一起）");
            }
        }
        SetSplash("正在等主程序就绪…");
        if (!await WaitPortAsync(_info.ConsolePort, TimeSpan.FromSeconds(30)))
            SetSplash("主程序还没就绪 —— 先把窗口开出来（⚙ 里能看日志）");

        // 界面从哪来：**优先本机那条"只发界面"的监听** —— 主进程从磁盘直接发，
        // 不经过唯一地址那条网关（网关没起 / 端口被占都不影响把界面打开）。
        _workspaceUrl = await ResolveWorkspaceAsync();
        await InitWebViewAsync();
    }

    private async Task<string> ResolveWorkspaceAsync()
    {
        var local = _info.LocalAssetsPort;
        if (local > 0)
        {
            for (var i = 0; i < 50; i++)          // 最多等 25 秒（主进程刚起时那条监听可能还没就绪）
            {
                if (await ProbeAsync($"http://127.0.0.1:{local}/healthz"))
                {
                    SetSplash($"界面装配就绪（本机 {local}）");
                    return $"http://127.0.0.1:{local}/console?embed=1";
                }
                await Task.Delay(500);
            }
        }
        return _info.ConsoleUrl + "?embed=1";
    }

    private static async Task<bool> ProbeAsync(string url)
    {
        try
        {
            using var c = new HttpClient { Timeout = TimeSpan.FromSeconds(3) };
            var r = await c.GetAsync(url);
            return r.IsSuccessStatusCode;
        }
        catch { return false; }
    }

    private static async Task<bool> WaitPortAsync(int port, TimeSpan limit)
    {
        var end = DateTime.UtcNow + limit;
        while (DateTime.UtcNow < end)
        {
            if (NativeWindowHelper.PortListening(port)) return true;
            await Task.Delay(300);
        }
        return NativeWindowHelper.PortListening(port);
    }

    private void SetSplash(string text)
    {
        if (_splashText is null) return;
        _splashText.Text = text;
    }

    private async Task InitWebViewAsync()
    {
        _view = new WebView2 { DefaultBackgroundColor = RailBar };
        Controls.Add(_view);
        Controls.SetChildIndex(_view, 0);     // 页面在最前；那一列在它左边、各占各的地方
        _view.BringToFront();
        LayoutChrome();

        var opts = new CoreWebView2EnvironmentOptions();
        if (DebugPort > 0)
            opts.AdditionalBrowserArguments =
                "--remote-debugging-port=" + DebugPort + " --remote-allow-origins=*";

        try
        {
            var env = await CoreWebView2Environment.CreateAsync(null, _info.WebViewDataDir, opts);
            await _view.EnsureCoreWebView2Async(env);
        }
        catch (Exception ex)
        {
            // ⚠️ 实测（2026-10-12，虚拟机）：同一个 WebView2 数据目录**只能被一个环境的进程组占用**。
            //    两个壳（比如一个在会话语、一个在 SSH 会话里起的）抢同一个目录时，第二个报
            //    「请求的资源在使用中 (0x800700AA)」—— 而单实例锁是 `Local\`（按会话隔离），拦不住。
            //    处置：换一个带会话号的数据目录重试一次；再不行才弹提示，并把两条路都写清楚。
            try
            {
                var alt = _info.WebViewDataDir + "-s" + System.Diagnostics.Process.GetCurrentProcess().SessionId;
                var env2 = await CoreWebView2Environment.CreateAsync(null, alt, opts);
                await _view.EnsureCoreWebView2Async(env2);
                BridgeNote("WebView2 数据目录被占用，已改用 " + alt);
            }
            catch (Exception ex2)
            {
                MessageBox.Show(this,
                    "枢阁的窗口用系统里的 Edge WebView2 渲染，装一次即可（微软官方，免费）：\n" +
                    "https://go.microsoft.com/fwlink/?LinkId=2124703\n\n" +
                    "如果已经装过，多半是**另一个枢阁窗口**正开着（同一个 Windows 会话/用户下只能开一个）：\n" +
                    "数据目录：" + _info.WebViewDataDir + "\n\n" +
                    "原始错误：" + ex.Message + "\n换目录重试也失败：" + ex2.Message,
                    "枢阁窗口起不来（WebView2）", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
        }

        var core = _view.CoreWebView2;
        var s = core.Settings;
        s.AreDefaultContextMenusEnabled = false;   // 右键留给界面自己
        s.AreDevToolsEnabled = true;               // F12 要能用
        s.IsStatusBarEnabled = false;
        s.AreBrowserAcceleratorKeysEnabled = true;
        s.IsZoomControlEnabled = false;            // 缩放由壳管（要记住）
        s.IsPasswordAutosaveEnabled = false;
        s.IsGeneralAutofillEnabled = false;

        // 应用内桥（页面 → 壳的命令）：走 WebView2 的消息通道。
        // 为什么不用 AddHostObjectToScript：本机实测注册不上（sync 里没有 shuge，取它报
        // 0x80070490），而且它既不抛异常也不生效 —— 失败看不见。消息通道一行就通。
        core.WebMessageReceived += (_, e) =>
        {
            try
            {
                var raw = e.TryGetWebMessageAsString();
                ShellBridge.Handle(raw, this);
            }
            catch (Exception ex) { BridgeNote("消息处理异常：" + ex.Message); }
        };
        BridgeNote("");

        core.NewWindowRequested += (_, e) => { e.Handled = true; OpenExternal(e.Uri); };
        core.NavigationStarting += (_, e) =>
        {
            if (!IsLocalUrl(e.Uri)) { e.Cancel = true; OpenExternal(e.Uri); }
        };

        _view.ZoomFactor = Math.Clamp(_state.Zoom, 0.5, 2.5);
        core.Navigate(_workspaceUrl.Length > 0 ? _workspaceUrl : _info.ConsoleUrl + "?embed=1");

        if (_splash is not null) { Controls.Remove(_splash); _splash.Dispose(); _splash = null; }
    }

    private static bool IsLocalUrl(string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var u)) return false;
        if (u.Scheme is "about" or "data" or "blob") return true;
        return u.Host is "127.0.0.1" or "localhost";
    }

    // ── 动作 ───────────────────────────────────────────────────────
    /// <summary>
    /// 切页签：调页面里的 showTab（同一份文档内切，不整页重载、也不动壳的地址）。
    /// 页签本身已经搬到壳这一列（网页顶栏在 embed 模式下是藏起来的）。
    /// </summary>
    private void Go(string tab)
    {
        HighlightNav(tab);
        ShowTabTitle(tab);
        try { _view?.CoreWebView2?.ExecuteScriptAsync($"showTab('{tab}')"); } catch { }
    }

    // ↓↓ 桥（ShugeHost）用的入口：都只是把壳自己那几个动作原样转出去，不另写一套实现
    public void BridgeZoom(double delta) => Zoom(delta);
    /// <summary>自检用：现在实际是多少倍（桥那边问一句就能验"点一下真的变了"）。</summary>
    public double BridgeZoomValue() => _view?.ZoomFactor ?? 0;
    public void BridgeZoomReset() => SetZoom(1.0);
    public void BridgeToggleFullscreen() => ToggleFullscreen();
    public void BridgeReload() { try { _view?.Reload(); } catch { } }
    public void BridgeDevTools() => ToggleDevTools();
    public void BridgeOpenInBrowser() => OpenInBrowser();
    public void BridgeOpenDataDir() => NativeWindowHelper.SpawnHidden(
        "explorer.exe", Path.Combine(_info.PlatformDir, "data"), _info.PlatformDir);
    /// <summary>打开回收站：逻辑在页面里（window.__shugeOpenBin），壳只叫它一声。</summary>
    private void OpenBin() => BridgeOpenBin();

    /// <summary>回收站：逻辑在页面里（哪一格能开、面板装配好没有，页面最清楚）。
    /// 壳只调一次；失败时页面会回一条 binFailed，壳**弹出来说清楚**（不静默）。</summary>
    public void BridgeOpenBin()
    {
        try { _view?.CoreWebView2?.ExecuteScriptAsync("window.__shugeOpenBin && window.__shugeOpenBin()"); }
        catch { }
    }

    public void BridgeOpenVaultDir()
    {
        var vault = ReadVaultPath();
        NativeWindowHelper.SpawnHidden("explorer.exe",
            Directory.Exists(vault) ? vault : _info.PlatformDir, _info.PlatformDir);
    }

    private void Zoom(double delta) => SetZoom((_view?.ZoomFactor ?? 1.0) + delta);

    private void SetZoom(double z)
    {
        if (_view is null) return;
        _view.ZoomFactor = Math.Clamp(z, 0.5, 2.5);
        _state.Zoom = _view.ZoomFactor;
        BridgeNote("");
    }

    private void OpenInBrowser() => OpenExternal(_view?.Source?.ToString() ?? _info.ConsoleUrl);

    private static void OpenExternal(string url)
    {
        try { NativeWindowHelper.SpawnHidden("rundll32.exe", $"url.dll,FileProtocolHandler {url}", Environment.CurrentDirectory); }
        catch { }
    }

    private void ToggleDevTools() => _view?.CoreWebView2?.OpenDevToolsWindow();

    private void ToggleFullscreen()
    {
        if (!_fullscreen)
        {
            _beforeFullscreen = WindowState;
            _beforeBorder = FormBorderStyle;
            _beforeBounds = Bounds;
            if (_rail is not null) _rail.Visible = false;
            FormBorderStyle = FormBorderStyle.None;
            WindowState = FormWindowState.Normal;
            Bounds = Screen.FromControl(this).Bounds;
            _fullscreen = true;
        }
        else
        {
            FormBorderStyle = _beforeBorder;
            Bounds = _beforeBounds;
            WindowState = _beforeFullscreen;
            if (_rail is not null) _rail.Visible = true;
            _fullscreen = false;
        }
        BridgeNote("");
    }

    private Icon? TryLoadIcon()
    {
        try
        {
            // 平台 bin\shuge.ico（scripts/make_icon.py 生成的那份：护眼底 + 「枢」）
            var ico = _info.IconFile;
            if (!File.Exists(ico)) ico = Path.Combine(AppContext.BaseDirectory, "shuge.ico");
            if (!File.Exists(ico)) return null;
            // 顺手做一张标题栏用的小位图（32px，高清屏也够）
            try
            {
                using var ic = new Icon(ico, 32, 32);
                TitleIcon = ic.ToBitmap();
            }
            catch { }
            return new Icon(ico);
        }
        catch { return null; }
    }

    // ── 键盘 ───────────────────────────────────────────────────────
    private void OnKeyDown(object? sender, KeyEventArgs e)
    {
        if (e.KeyCode == Keys.F11) { ToggleFullscreen(); e.Handled = true; e.SuppressKeyPress = true; return; }
        if (e.KeyCode == Keys.F12) { ToggleDevTools(); e.Handled = true; e.SuppressKeyPress = true; return; }
        if (e.KeyCode == Keys.Escape && _fullscreen) { ToggleFullscreen(); e.Handled = true; return; }
        if (e.Control && e.Shift && e.KeyCode == Keys.B) { OpenInBrowser(); e.Handled = true; return; }
        if (e.Control && (e.KeyCode == Keys.Oemplus || e.KeyCode == Keys.Add)) { Zoom(+0.1); e.Handled = true; return; }
        if (e.Control && (e.KeyCode == Keys.OemMinus || e.KeyCode == Keys.Subtract)) { Zoom(-0.1); e.Handled = true; return; }
        if (e.Control && e.KeyCode == Keys.D0) { SetZoom(1.0); e.Handled = true; return; }
        // Ctrl+1/2/3：切三个页签（现代软件的手感）
        if (e.Control && e.KeyCode == Keys.D1) { Go("console"); e.Handled = true; return; }
        if (e.Control && e.KeyCode == Keys.D2) { Go("notes"); e.Handled = true; return; }
        if (e.Control && e.KeyCode == Keys.D3) { Go("core"); e.Handled = true; return; }
        if (e.Control && e.KeyCode == Keys.D4) { Go("settings"); e.Handled = true; }
    }

    // ── 记住窗口状态 ───────────────────────────────────────────────
    private System.Windows.Forms.Timer? _stateTimer;

    /// <summary>延时 800ms 合并保存（拖动/缩放时不要每次都写盘）。</summary>
    private void SaveStateSoon()
    {
        if (_stateTimer is null)
        {
            _stateTimer = new System.Windows.Forms.Timer { Interval = 800 };
            _stateTimer.Tick += (_, _) => { _stateTimer!.Stop(); SaveStateNow(); };
        }
        _stateTimer.Stop();
        _stateTimer.Start();
    }

    protected override void OnResize(EventArgs e)
    {
        base.OnResize(e);
        LayoutChrome();
        _title?.Refresh();
    }

    protected override void OnLayout(LayoutEventArgs levent)
    {
        base.OnLayout(levent);
        LayoutChrome();
    }

    /// <summary>
    /// 自己排外壳那几块（标题栏 / 左边那一列 / 内容区）。
    /// ⚠️ 不用 Dock：本机 PerMonitorV2 + DPI=120 下 Dock 算出来的位置对不上
    /// （实测标题栏被排到 x=44、宽 1150，右边那三个窗口键直接跑到可视区外 —— 2026-10-11）。
    /// 这里按"逻辑像素 × 当前缩放"显式算，永远对得上。
    /// </summary>
    private void LayoutChrome()
    {
        // ⚠️ 关键一条（2026-10-11 查了很久）：这个进程的坐标**不等于物理像素**。
        //    本机 DPI=125%：WinForms 的 ClientSize 是 1360x900，而系统量到的窗口只有 1088x720
        //    （1088 = 1360 × 0.8 = 1360 ÷ 1.25）。也就是说进程里说的 1 个"像素"，
        //    在屏幕上是 1.25 个物理像素。
        //    所以外壳几何**不能**用 ClientSize，要用 Win32 量到的物理尺寸；子控件的 Bounds
        //    按"物理 ÷ 这个比例"折算回去。这样算出来的那一列与三个窗口键，落在真正的窗口里。
        var w32 = Win32ClientSize();
        var fx = w32.Width > 0 ? ClientSize.Width / (double)w32.Width : 1.0;
        var fy = w32.Height > 0 ? ClientSize.Height / (double)w32.Height : 1.0;

        var titleHp = Px(TitleBar.HeightLogical, DeviceDpi / 96f);   // 物理像素
        var railWp = Px(RailWidthLogical, DeviceDpi / 96f);          // 物理像素
        var titleH = Math.Max(1, (int)Math.Round(titleHp / fy));
        var railW = Math.Max(1, (int)Math.Round(railWp / fx));
        var w = ClientSize.Width;
        var h = ClientSize.Height;

        if (_title is not null) _title.Bounds = new Rectangle(0, 0, w, titleH);
        if (_rail is not null) _rail.Bounds = new Rectangle(0, titleH, railW, Math.Max(0, h - titleH));
        if (_view is not null) _view.Bounds = new Rectangle(railW, titleH, Math.Max(0, w - railW), Math.Max(0, h - titleH));

        LayoutRailItems(railW);
    }

    private static int Px(int logical, double scale) => Math.Max(1, (int)Math.Round(logical * scale));

    /// <summary>进程坐标 ÷ 物理像素 的比例（本机 125% 时 ≈ 1.25）。</summary>
    private double CoordScale
    {
        get
        {
            var w32 = Win32ClientSize();
            return w32.Width > 0 ? ClientSize.Width / (double)w32.Width : 1.0;
        }
    }

    /// <summary>问 Windows 要客户区的**物理**像素尺寸（ClientSize 是进程坐标，不等它）。</summary>
    private Size Win32ClientSize()
    {
        var r = new NativeRect();
        if (GetClientRect(Handle, ref r)) return new Size(r.Right - r.Left, r.Bottom - r.Top);
        return new Size(0, 0);
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool GetClientRect(IntPtr hWnd, ref NativeRect r);



    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    private struct NativeRect { public int Left, Top, Right, Bottom; }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool GetWindowRect(IntPtr hWnd, ref NativeRect r);

    /// <summary>那一列里的四个页签与最底下那颗设置键：按物理像素排。</summary>
    private void LayoutRailItems(int railW)
    {
        if (_rail is null) return;
        var s = (DeviceDpi / 96f) / CoordScale;
        var pad = Px(6, s);
        var btnW = Math.Max(1, railW - pad * 2);
        var btnH = Px(44, s);
        var y = Px(10, s);
        foreach (var b in _navBtns.Values)
        {
            b.Bounds = new Rectangle(pad, y, btnW, btnH);
            y += btnH + Px(4, s);
        }
        if (_gear is not null)
            _gear.Bounds = new Rectangle(pad, Math.Max(0, _rail.Height - btnH - Px(10, s)), btnW, btnH);
    }

    private void OnFormClosing(object? sender, FormClosingEventArgs e) => SaveStateNow();

    private void SaveStateNow()
    {
        try
        {
            var b = _fullscreen ? _beforeBounds
                : WindowState == FormWindowState.Normal ? Bounds : RestoreBounds;
            if (b.Width > 200 && b.Height > 200)
            {
                // 逻辑像素 → 物理像素（跟恢复那边对称；自检用 user32 量的就是物理值）
                var sc = DeviceDpi / 96f;
                _state.X = (int)Math.Round(b.X * sc);
                _state.Y = (int)Math.Round(b.Y * sc);
                _state.W = (int)Math.Round(b.Width * sc);
                _state.H = (int)Math.Round(b.Height * sc);
            }
            _state.Maximized = WindowState == FormWindowState.Maximized;
            _state.Zoom = _view?.ZoomFactor ?? _state.Zoom;
            _info.SaveState(_state);
        }
        catch { }
    }
}
