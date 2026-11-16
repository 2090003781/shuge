namespace Shuge.Desktop;

/// <summary>
/// 壳的配色（跟页面那套 CSS 变量对齐）。浅色 = 护眼浅黄；深色先摆好变量。
/// 放在一个类里：标题栏、左边那一列、设置页的弹菜单都读同一份，不各写一套。
/// </summary>
internal sealed class ShellPalette
{
    public Color Bg { get; private set; } = Color.FromArgb(0xF7, 0xF0, 0xDE);        // --bar
    public Color Line { get; private set; } = Color.FromArgb(0xE2, 0xD8, 0xBC);      // --line3
    public Color Fg { get; private set; } = Color.FromArgb(0x4A, 0x44, 0x33);
    public Color Dim { get; private set; } = Color.FromArgb(0x8A, 0x82, 0x6A);
    public Color Hover { get; private set; } = Color.FromArgb(0xF1, 0xE7, 0xCF);     // --hover2
    public Color Active { get; private set; } = Color.FromArgb(0xD7, 0xE5, 0xF8);    // --primary
    public Color ActiveFg { get; private set; } = Color.FromArgb(0x1F, 0x4E, 0x79);  // --on-primary
    public Color MenuBg { get; private set; } = Color.FromArgb(0xFF, 0xFC, 0xF3);

    public void Use(string theme)
    {
        if (!string.Equals(theme, "dark", StringComparison.OrdinalIgnoreCase)) return;   // light 就是初值
        Bg = Color.FromArgb(0x20, 0x23, 0x3A);
        Line = Color.FromArgb(0x2E, 0x33, 0x45);
        Fg = Color.FromArgb(0xC8, 0xD2, 0xF0);
        Dim = Color.FromArgb(0x8A, 0x93, 0xB8);
        Hover = Color.FromArgb(0x25, 0x2A, 0x3B);
        Active = Color.FromArgb(0x2F, 0x4A, 0x6E);
        ActiveFg = Color.FromArgb(0xDC, 0xEA, 0xFF);
        MenuBg = Color.FromArgb(0x1F, 0x23, 0x35);
    }
}
