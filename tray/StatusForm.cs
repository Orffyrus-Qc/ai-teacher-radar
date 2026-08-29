using System.Drawing.Drawing2D;

namespace RadarTray;

/// <summary>Compact live dashboard. Closing it hides it; the tray keeps polling.</summary>
public sealed class StatusForm : Form
{
    private readonly Label _headline = new();
    private readonly Label _detail = new();
    private readonly Panel _gpuPanel = new();
    private readonly ListView _schedule = new();
    private readonly Panel _dot = new();

    public StatusForm()
    {
        Text = "AI Teacher Radar";
        Size = new Size(520, 460);
        MinimumSize = new Size(460, 400);
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Color.FromArgb(24, 26, 30);
        ForeColor = Color.FromArgb(232, 236, 240);
        Font = new Font("Segoe UI", 9f);
        ShowInTaskbar = false;

        _dot.SetBounds(18, 18, 18, 18);
        _dot.Paint += (_, e) =>
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            using var b = new SolidBrush(_dot.BackColor);
            e.Graphics.FillEllipse(b, 0, 0, 17, 17);
        };
        _dot.BackColor = Color.Gray;

        _headline.SetBounds(46, 14, 440, 26);
        _headline.Font = new Font("Segoe UI Semibold", 13f);

        _detail.SetBounds(20, 48, 466, 116);
        _detail.ForeColor = Color.FromArgb(176, 186, 197);

        var gpuTitle = Section("GPU", 20, 172);
        _gpuPanel.SetBounds(20, 194, 466, 74);
        _gpuPanel.Paint += PaintGpus;

        var schedTitle = Section("Next scheduled runs", 20, 276);
        _schedule.SetBounds(20, 298, 466, 112);
        _schedule.View = View.Details;
        _schedule.FullRowSelect = true;
        _schedule.BackColor = Color.FromArgb(32, 35, 40);
        _schedule.ForeColor = Color.FromArgb(216, 224, 232);
        _schedule.BorderStyle = BorderStyle.None;
        _schedule.Columns.Add("Job", 170);
        _schedule.Columns.Add("Next run", 280);

        Controls.AddRange([_dot, _headline, _detail, gpuTitle, _gpuPanel, schedTitle, _schedule]);

        FormClosing += (_, e) =>
        {
            if (e.CloseReason == CloseReason.UserClosing)
            {
                e.Cancel = true;   // keep polling in the tray
                Hide();
            }
        };
    }

    private static Label Section(string text, int x, int y) => new()
    {
        Text = text.ToUpperInvariant(),
        Bounds = new Rectangle(x, y, 300, 18),
        ForeColor = Color.FromArgb(126, 138, 150),
        Font = new Font("Segoe UI Semibold", 8f)
    };

    private IReadOnlyList<GpuInfo> _gpus = [];

    public void Render(RadarStatus s)
    {
        _dot.BackColor = IconFactory.ColorFor(s.State);
        _dot.Invalidate();
        _headline.Text = s.Headline;
        _headline.ForeColor = IconFactory.ColorFor(s.State);

        var lines = new List<string>
        {
            $"Container    {s.ContainerStatus}",
            $"Worker       {s.WorkerPhase}{(s.CurrentJob is null ? "" : $"  ({s.CurrentJob})")}",
            $"Queue        {s.Waiting} waiting" +
                (s.DeferredByGpu > 0 ? $", {s.DeferredByGpu} deferred by GPU" : ""),
            $"GPU          {s.GpuSummary}",
            $"Scheduler    {s.SchedulerMode}",
            $"LLM          {(s.LlmReady ? "ready" : "not ready")} — {s.LlmDetail}"
        };
        if (s.Error is not null) lines.Add($"Error        {s.Error}");
        _detail.Text = string.Join(Environment.NewLine, lines);

        _gpus = s.Gpus;
        _gpuPanel.Invalidate();

        _schedule.BeginUpdate();
        _schedule.Items.Clear();
        foreach (var (id, next) in s.Upcoming)
            _schedule.Items.Add(new ListViewItem([id, Pretty(next)]));
        _schedule.EndUpdate();
    }

    private static string Pretty(string iso) =>
        DateTimeOffset.TryParse(iso, out var dt)
            ? $"{dt.LocalDateTime:ddd HH:mm}  (in {Humanise(dt - DateTimeOffset.Now)})"
            : iso;

    private static string Humanise(TimeSpan t) =>
        t.TotalMinutes < 1 ? "<1 min"
        : t.TotalHours < 1 ? $"{t.TotalMinutes:F0} min"
        : t.TotalDays < 1 ? $"{t.Hours}h {t.Minutes}m"
        : $"{t.Days}d {t.Hours}h";

    private void PaintGpus(object? sender, PaintEventArgs e)
    {
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        if (_gpus.Count == 0)
        {
            using var dim = new SolidBrush(Color.FromArgb(120, 130, 140));
            g.DrawString("no telemetry (start the container to read nvidia-smi)",
                Font, dim, 0, 4);
            return;
        }

        var y = 0;
        foreach (var gpu in _gpus)
        {
            var pctVram = gpu.VramTotalMib == 0 ? 0f : (float)gpu.VramUsedMib / gpu.VramTotalMib;
            var label = $"GPU{gpu.Index}  {gpu.Name}";
            var stats = $"{gpu.UtilPct}%  ·  {gpu.VramUsedMib:N0}/{gpu.VramTotalMib:N0} MiB";

            using var text = new SolidBrush(Color.FromArgb(200, 210, 220));
            g.DrawString(label, Font, text, 0, y);
            var size = g.MeasureString(stats, Font);
            g.DrawString(stats, Font, text, _gpuPanel.Width - size.Width, y);

            var barY = y + 19;
            using var track = new SolidBrush(Color.FromArgb(45, 49, 56));
            g.FillRectangle(track, 0, barY, _gpuPanel.Width, 6);

            // Busy is judged by utilisation first, so colour by that.
            var colour = gpu.UtilPct >= 25 ? Color.FromArgb(243, 156, 18)
                                           : Color.FromArgb(46, 204, 113);
            using var fill = new SolidBrush(colour);
            g.FillRectangle(fill, 0, barY, _gpuPanel.Width * pctVram, 6);

            y += 37;
        }
    }
}
