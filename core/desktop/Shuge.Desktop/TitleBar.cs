using System.Drawing.Drawing2D;

namespace Shuge.Desktop;

/// <summary>
/// 窗口最上边那一条（现代软件的样子）：品牌 + 当前位置 + 三个窗口键。
///
/// 辞玖 2026-10-11 11:34：「可以仿照 Obsidian 利用一下最上边那个条空间」。
/// Obsidian 的做法就是**自己画标题栏**（Brand + 当前文件 + 最小化/最大化/关闭），
/// 把系统那条又厚又空的白条换掉。这里照做，并且：
///   · 那条**空白处可以拖窗口**（交给系统的标题栏拖动：WM_NCLBUTTONDOWN + HTCAPTION）；
///   · 双击空白处 = 最大化/还原（同上，系统行为）；
///   · 最大化那一下会把鼠标位置交给系统 → Win11 的"贴靠布局"照样弹（HTCAPTION 的标准收益）；
///   · 整个窗口表单改成无边框（FormBorderStyle.None），边框/拉伸由 MainForm 的 WM_NCHITTEST 管。
/// </summary>
internal sealed class TitleBar : Control
{
    public const int BtnW = 46;            // 逻辑宽；物理宽见 BtnWPx
    public const int HeightLogical = 40;   // 逻辑高（物理高由 MainForm.LayoutChrome 按缩放算）

    private readonly MainForm _owner;
    private bool _closeHot, _maxHot, _minHot;

    public TitleBar(MainForm owner)
    {
        _owner = owner;
        // ⚠️ 关掉自动缩放、也不用 Dock：本控件的 Bounds 由 MainForm.LayoutChrome 按
        //    **物理像素**算好；再被 WinForms 缩放一次就会宽出 1.25 倍（1493 vs 窗口 1194），
        //    右边那三个窗口键直接画到窗口外面（2026-10-11 踩了很久）。
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.UserPaint |
                 ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
    }

    /// <summary>
    /// 不许父窗体替我们缩放：本控件的 Bounds 由 MainForm.LayoutChrome **按物理像素**算好，
    /// 再缩一次就宽出 1.25 倍（实测 1493 vs 窗口 1194），右边那三个窗口键会画到窗口外面。
    /// </summary>
    protected override void ScaleControl(SizeF factor, BoundsSpecified specified)
    {
        // 故意不调 base：几何由 LayoutChrome 说了算
    }

    /// <summary>三个键的物理宽（按这一条的实际高度换算，跟窗口缩放一致）。</summary>
    private int BtnWPx => Math.Max(20, (int)Math.Round(BtnW * Height / (double)HeightLogical));
    private Rectangle MinRect => new(Width - BtnWPx * 3, 0, BtnWPx, Height);
    private Rectangle MaxRect => new(Width - BtnWPx * 2, 0, BtnWPx, Height);
    private Rectangle CloseRect => new(Width - BtnWPx, 0, BtnWPx, Height);

    protected override void OnMouseMove(MouseEventArgs e)
    {
        var c = CursorFor(e.Location);
        if (Cursor != c) Cursor = c;
        base.OnMouseMove(e);
    }

    protected override void OnMouseLeave(EventArgs e)
    {
        _closeHot = _maxHot = _minHot = false;
        Invalidate();
        base.OnMouseLeave(e);
    }

    private Cursor CursorFor(Point p)
    {
        if (CloseRect.Contains(p) || MaxRect.Contains(p) || MinRect.Contains(p)) return Cursors.Hand;
        return Cursors.Default;
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        var g = e.Graphics;
        try
        {
            System.IO.File.AppendAllText(@"D:\core\data\tmp\titlebar-paint.txt",
                string.Format("{0:HH:mm:ss} paint w={1} h={2} minX={3} closeX={4}\n",
                    DateTime.Now, Width, Height, MinRect.X, CloseRect.X));
        }
        catch { }
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.ClearTypeGridFit;

        using (var b = new SolidBrush(_owner.RailBg)) g.FillRectangle(b, ClientRectangle);
        using (var pen = new Pen(_owner.RailLine))
            g.DrawLine(pen, 0, Height - 1, Width, Height - 1);   // 下沿一条细线

        // 品牌 + 当前位置（"在哪儿"这件事，Obsidian 也是写在标题栏里的）。
        // 字号与留白都按窗口缩放放大 —— TitleFont 是逻辑字号，TextRenderer 不替我们缩。
        var f = _owner.ScaledFont(_owner.TitleFont);
        var f2 = _owner.ScaledFont(_owner.TitleSubFont);
        var pad = (int)Math.Round(14 * Height / (double)HeightLogical);

        // 图标（"枢"）：系统标题栏换掉之后这里得自己画一个，不然左上角就秃了
        var ico = _owner.TitleIcon;
        if (ico is not null)
        {
            var side = Math.Max(16, (int)Math.Round(18 * Height / (double)HeightLogical));
            g.DrawImage(ico, new Rectangle(pad, (Height - side) / 2, side, side));
            pad += side + (int)Math.Round(8 * Height / (double)HeightLogical);
        }

        TextRenderer.DrawText(g, "枢阁", f, new Point(pad, (Height - f.Height) / 2),
            _owner.RailActiveFg, TextFormatFlags.NoPadding);
        var bx = pad + TextRenderer.MeasureText(g, "枢阁", f, new Size(999, 99),
            TextFormatFlags.NoPadding).Width + (int)Math.Round(10 * Height / (double)HeightLogical);
        TextRenderer.DrawText(g, "·  " + _owner.TabTitle, f2,
            new Point(bx, (Height - f2.Height) / 2), _owner.RailDim, TextFormatFlags.NoPadding);

        // 三个窗口键
        if (_minHot) using (var b = new SolidBrush(_owner.RailHover)) g.FillRectangle(b, MinRect);
        if (_maxHot) using (var b = new SolidBrush(_owner.RailHover)) g.FillRectangle(b, MaxRect);
        if (_closeHot) using (var b = new SolidBrush(Color.FromArgb(0xE8, 0x11, 0x23))) g.FillRectangle(b, CloseRect);

        var ink = _owner.RailFg;
        try
        {
            System.IO.File.AppendAllText(@"D:\core\data\tmp\titlebar-paint.txt",
                string.Format("{0:HH:mm:ss} buttons ink={1} min={2} max={3} close={4}\n",
                    DateTime.Now, ink, MinRect, MaxRect, CloseRect));
        }
        catch { }
        using (var pen = new Pen(ink, 1.2f))
        {
            var c = Center(MinRect);
            g.DrawLine(pen, c.X - 5, c.Y, c.X + 5, c.Y);                       // ─
            c = Center(MaxRect);
            if (_owner.WindowState == FormWindowState.Maximized)
            {
                g.DrawRectangle(pen, c.X - 6, c.Y - 4, 9, 8);                  // ❐（还原）
                g.DrawRectangle(pen, c.X - 3, c.Y - 6.5f, 9, 8);
            }
            else
            {
                g.DrawRectangle(pen, c.X - 5, c.Y - 5, 10, 10);                // □
            }
            c = Center(CloseRect);
            using var penX = new Pen(_closeHot ? Color.White : ink, 1.2f);
            g.DrawLine(penX, c.X - 5, c.Y - 5, c.X + 5, c.Y + 5);              // ✕
            g.DrawLine(penX, c.X + 5, c.Y - 5, c.X - 5, c.Y + 5);
        }
    }

    private static Point Center(Rectangle r) => new(r.X + r.Width / 2, r.Y + r.Height / 2);

    protected override void OnMouseDown(MouseEventArgs e)
    {
        if (e.Button != MouseButtons.Left) { base.OnMouseDown(e); return; }
        var p = e.Location;
        if (CloseRect.Contains(p)) { _owner.Close(); return; }
        if (MaxRect.Contains(p)) { _owner.CaptionMaximize(); return; }
        if (MinRect.Contains(p)) { _owner.CaptionMinimize(); return; }

        // 空白处：交给系统的标题栏拖动（双击 = 最大化/还原；拖到屏幕边 = 贴靠）
        NativeWindowHelper.SystemCaptionDrag(Handle);
    }

    protected override void OnMouseUp(MouseEventArgs e) { base.OnMouseUp(e); }

    /// <summary>鼠标在哪一个键上（MainForm 的 WM_NCHITTEST 用它把消息"让"给这三个键）。</summary>
    public bool HitButton(Point clientPt) =>
        CloseRect.Contains(clientPt) || MaxRect.Contains(clientPt) || MinRect.Contains(clientPt);

    /// <summary>悬停状态：由 MainForm 的 WM_NCHITTEST 那条路维护（因为鼠标事件可能被系统接管）。</summary>
    public void SetHot(Point? clientPt)
    {
        var p = clientPt ?? new Point(-1, -1);
        var ch = CloseRect.Contains(p);
        var mh = MaxRect.Contains(p);
        var nh = MinRect.Contains(p);
        if (ch == _closeHot && mh == _maxHot && nh == _minHot) return;
        _closeHot = ch; _maxHot = mh; _minHot = nh;
        Invalidate();
    }

    /// <summary>状态变了（最大化/还原）重画那三个键。</summary>
    public void Refresh() => Invalidate();
}
