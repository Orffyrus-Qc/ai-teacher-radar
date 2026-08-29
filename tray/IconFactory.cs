using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;

namespace RadarTray;

/// <summary>
/// Icons are drawn rather than shipped as files, so the exe stays a single
/// self-contained artefact. One icon per state, built once and cached — GDI
/// handles from GetHicon must be destroyed explicitly, so they are never
/// created per poll.
/// </summary>
public static class IconFactory
{
    // DllImport rather than LibraryImport: the source generator would force
    // AllowUnsafeBlocks on the whole project for one call.
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool DestroyIcon(IntPtr handle);

    private static readonly Dictionary<RadarState, Icon> Cache = new();

    public static Color ColorFor(RadarState state) => state switch
    {
        RadarState.Idle => Color.FromArgb(46, 204, 113),   // green  - alive
        RadarState.Working => Color.FromArgb(52, 152, 219),   // blue   - busy working
        RadarState.DeferredGpu => Color.FromArgb(243, 156, 18),   // amber  - waiting on GPU
        RadarState.Unhealthy => Color.FromArgb(231, 76, 60),    // red    - up but broken
        RadarState.Starting => Color.FromArgb(155, 89, 182),   // purple - coming up
        RadarState.Stopped => Color.FromArgb(127, 140, 141),  // grey   - stopped
        _ => Color.FromArgb(127, 140, 141)
    };

    public static Icon For(RadarState state)
    {
        if (Cache.TryGetValue(state, out var cached)) return cached;

        using var bmp = new Bitmap(32, 32);
        using (var g = Graphics.FromImage(bmp))
        {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.Clear(Color.Transparent);

            var colour = ColorFor(state);
            // Radar dish: a filled disc with two sweep arcs, so the icon reads
            // as "monitoring" and not just "a coloured dot".
            using var fill = new SolidBrush(colour);
            g.FillEllipse(fill, 3, 3, 26, 26);

            using var ring = new Pen(Color.FromArgb(210, Color.White), 2f);
            g.DrawArc(ring, 8, 8, 16, 16, 200, 140);
            g.DrawArc(ring, 12, 12, 8, 8, 200, 140);

            if (state == RadarState.Stopped)
            {
                using var bar = new Pen(Color.FromArgb(235, Color.White), 3f);
                g.DrawLine(bar, 9, 23, 23, 9);
            }
        }

        var handle = bmp.GetHicon();
        try
        {
            // Clone off the handle so we can release it immediately; the clone
            // owns managed memory only.
            using var temp = Icon.FromHandle(handle);
            var icon = (Icon)temp.Clone();
            Cache[state] = icon;
            return icon;
        }
        finally
        {
            DestroyIcon(handle);
        }
    }
}
