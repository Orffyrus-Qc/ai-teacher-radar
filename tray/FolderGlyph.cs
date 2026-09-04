using System.Drawing.Drawing2D;

namespace RadarTray;

/// <summary>Tiny folder icon for the downloads toolbar button.</summary>
internal static class FolderGlyph
{
    public static Bitmap Small()
    {
        var bmp = new Bitmap(16, 16);
        using var g = Graphics.FromImage(bmp);
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.Clear(Color.Transparent);
        using var tab = new SolidBrush(Color.FromArgb(230, 180, 70));
        using var body = new SolidBrush(Color.FromArgb(245, 196, 88));
        using var edge = new Pen(Color.FromArgb(160, 110, 30));
        g.FillRectangle(tab, 1, 3, 6, 3);
        g.FillRectangle(body, 1, 5, 14, 10);
        g.DrawRectangle(edge, 1, 5, 13, 9);
        g.DrawLine(edge, 1, 5, 7, 5);
        return bmp;
    }
}
