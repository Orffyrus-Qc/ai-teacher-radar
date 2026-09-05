using System.Diagnostics;
using System.Threading;

namespace RadarTray;

public sealed class TrayApp : ApplicationContext
{
    private readonly AppConfig _cfg;
    private readonly RadarClient _client;
    private readonly NotifyIcon _tray;
    private readonly System.Windows.Forms.Timer _timer;
    private readonly StatusForm _form = new();
    private readonly ReportsForm _reports;

    private RadarState _lastState = RadarState.Unknown;
    private bool _modelsLoaded;
    private bool _polling;
    private bool _notifyDownloads;
    private readonly HashSet<string> _seenDownloads = [];
    private readonly EventWaitHandle? _showEvent;
    private int _showRequested;

    private readonly ToolStripMenuItem _miStart = new("Start");
    private readonly ToolStripMenuItem _miStop = new("Stop");
    private readonly ToolStripMenuItem _miRestart = new("Restart");
    private readonly ToolStripMenuItem _miStatusLine = new("Polling…") { Enabled = false };

    public TrayApp(AppConfig cfg, bool openWindow = true, EventWaitHandle? showEvent = null)
    {
        _cfg = cfg;
        _showEvent = showEvent;
        _client = new RadarClient(cfg);
        _reports = new ReportsForm(cfg);
        _reports.ModelChanged += m =>
        {
            // Remember the choice so it survives a restart.
            _cfg.PreferredModel = m;
            _cfg.Save();
        };

        var menu = new ContextMenuStrip();
        menu.Items.Add(_miStatusLine);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(_miStart);
        menu.Items.Add(_miStop);
        menu.Items.Add(_miRestart);
        menu.Items.Add(new ToolStripSeparator());

        var run = new ToolStripMenuItem("Run now");
        run.DropDownItems.Add(Job("Search", "search"));
        run.DropDownItems.Add(Job("Daily synthesis", "daily"));
        run.DropDownItems.Add(Job("Weekly synthesis", "weekly"));
        menu.Items.Add(run);

        menu.Items.Add(new ToolStripMenuItem("Open app window", null, (_, _) => ShowReports())
        { Font = new Font(menu.Font, FontStyle.Bold) });
        menu.Items.Add(new ToolStripMenuItem("Open briefs folder", null,
            (_, _) => OpenPath(_cfg.OutputFolder)));
        menu.Items.Add(new ToolStripMenuItem("Open downloads folder", null,
            (_, _) => OpenPath(_cfg.DownloadsFolder)));
        menu.Items.Add(new ToolStripMenuItem("Open API health", null,
            (_, _) => OpenPath($"{_cfg.BaseUrl}/health")));
        menu.Items.Add(new ToolStripMenuItem("Radar status (diagnostics)…", null,
            (_, _) => ShowStatus()));
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(new ToolStripMenuItem("Exit", null, (_, _) => Exit()));

        _miStart.Click += async (_, _) => await DoDockerAsync("Start", _client.StartAsync);
        _miStop.Click += async (_, _) => await DoDockerAsync("Stop", _client.StopAsync);
        _miRestart.Click += async (_, _) => await DoDockerAsync("Restart", _client.RestartAsync);

        _tray = new NotifyIcon
        {
            Icon = IconFactory.For(RadarState.Unknown),
            Text = "AI Teacher Radar — polling…",
            Visible = true,
            ContextMenuStrip = menu
        };
        // Opening from the tray must give the same window as launching the
        // app; two different-looking windows read as two different builds.
        _tray.DoubleClick += (_, _) => ShowReports();
        // A "run finished" balloon is an invitation to read the brief.
        _tray.BalloonTipClicked += (_, _) =>
        {
            if (_notifyDownloads)
            {
                _notifyDownloads = false;
                _reports.ShowDownloadsTab();
            }
            else ShowReports();
        };

        _timer = new System.Windows.Forms.Timer { Interval = Math.Max(2, cfg.PollSeconds) * 1000 };
        _timer.Tick += async (_, _) => await PollAsync();
        _timer.Start();

        if (_showEvent is not null)
        {
            var watch = new Thread(WatchShow) { IsBackground = true, Name = "radar-tray-show" };
            watch.Start();
            var pulse = new System.Windows.Forms.Timer { Interval = 150 };
            pulse.Tick += (_, _) =>
            {
                if (Interlocked.Exchange(ref _showRequested, 0) == 1)
                    ShowReports();
            };
            pulse.Start();
        }

        _ = PollAsync();
        if (openWindow) ShowReports();
    }

    private void WatchShow()
    {
        while (true)
        {
            try
            {
                if (_showEvent is null)
                    return;
                if (_showEvent.WaitOne(Timeout.Infinite))
                    Interlocked.Exchange(ref _showRequested, 1);
            }
            catch (ObjectDisposedException)
            {
                return;
            }
        }
    }

    private ToolStripMenuItem Job(string label, string kind) =>
        new(label, null, async (_, _) =>
        {
            var model = _reports.SelectedModel;
            var (ok, body) = await _client.PostJobAsync(kind, model);
            var title = model is null ? label : $"{label} · {model}";
            Notify(ok ? $"{title} queued" : $"{title} failed",
                ok ? Summarise(body) : body,
                ok ? ToolTipIcon.Info : ToolTipIcon.Error);
            await PollAsync();
        });

    private static string Summarise(string body)
    {
        // The API answers with the job plus a plain-English GPU note; that note
        // is the only part worth a balloon.
        var marker = "\"note\":\"";
        var i = body.IndexOf(marker, StringComparison.Ordinal);
        if (i < 0) return "queued";
        var start = i + marker.Length;
        var end = body.IndexOf('"', start);
        return end > start ? body[start..end].Replace("\\u2014", "—") : "queued";
    }

    private async Task DoDockerAsync(string what, Func<Task<(bool, string)>> action)
    {
        SetBusy(true);
        _miStatusLine.Text = $"{what}…";
        var (ok, output) = await action();
        SetBusy(false);
        if (!ok)
            Notify($"{what} failed", Tail(output), ToolTipIcon.Error);
        await PollAsync();
    }

    private static string Tail(string text)
    {
        var lines = text.Split('\n', StringSplitOptions.RemoveEmptyEntries);
        return lines.Length == 0 ? "no output" : lines[^1].Trim();
    }

    private void SetBusy(bool busy)
    {
        _miStart.Enabled = _miStop.Enabled = _miRestart.Enabled = !busy;
    }

    private async Task PollAsync()
    {
        if (_polling) return;          // a slow docker ps must not stack up
        _polling = true;
        try
        {
            var status = await _client.PollAsync(includeGpuDetail: _form.Visible);

            _tray.Icon = IconFactory.For(status.State);
            _tray.Text = Truncate($"AI Teacher Radar — {status.Headline}");
            _miStatusLine.Text = status.Headline;
            _miStart.Enabled = status.State == RadarState.Stopped;
            _miStop.Enabled = status.State != RadarState.Stopped;
            _miRestart.Enabled = status.State != RadarState.Stopped;

            // Fill the picker once the container is answering. Ollama may be
            // slower to come up than the radar, so retry until it returns some.
            if (!_modelsLoaded && status.State != RadarState.Stopped)
            {
                var (names, fallback) = await _client.GetModelsAsync();
                if (names.Count > 0)
                {
                    _reports.SetModels(names, fallback, _cfg.PreferredModel);
                    _modelsLoaded = true;
                }
            }

            if (_form.Visible) _form.Render(status);
            if (_reports.Visible) _reports.SetState(status.StateLine, IconFactory.ColorFor(status.State));

            var (dlOk, snap) = await _client.GetDownloadsAsync();
            if (dlOk)
            {
                _reports.BindDownloads(snap.Rows, snap.Folder, snap.HfAuth);
                foreach (var row in snap.Rows)
                {
                    var key = $"{row.Id}:{row.Status}";
                    if (!_seenDownloads.Add(key) || _lastState == RadarState.Unknown)
                        continue;
                    var (title, icon) = row.Status switch
                    {
                        "queued" or "downloading" => ("Leaked/drop model", ToolTipIcon.Info),
                        "done" => ("Download finished", ToolTipIcon.Info),
                        "refused" => ("Leak seen — not downloaded", ToolTipIcon.Warning),
                        "failed" => ("Download failed", ToolTipIcon.Error),
                        _ => ("", ToolTipIcon.None)
                    };
                    if (title.Length == 0) continue;
                    _notifyDownloads = true;
                    Notify(title, row.ModelId ?? row.Title, icon);
                }
            }

            if (_cfg.NotifyOnStateChange && _lastState != RadarState.Unknown
                && status.State != _lastState)
            {
                var (title, body, icon) = status.State switch
                {
                    RadarState.DeferredGpu =>
                        ("Run deferred", status.GpuSummary, ToolTipIcon.Warning),
                    RadarState.Unhealthy =>
                        ("Radar unhealthy", status.Error ?? status.ContainerStatus, ToolTipIcon.Error),
                    RadarState.Stopped =>
                        ("Radar stopped", status.ContainerStatus, ToolTipIcon.Warning),
                    RadarState.Idle when _lastState is RadarState.Working or RadarState.DeferredGpu =>
                        ("Run finished", "Brief written to the out folder.", ToolTipIcon.Info),
                    _ => ("", "", ToolTipIcon.None)
                };
                if (title.Length > 0) Notify(title, body, icon);
                if (status.State == RadarState.Idle && _reports.Visible) _reports.Reload();
            }
            _lastState = status.State;
        }
        finally
        {
            _polling = false;
        }
    }

    private void ShowReports()
    {
        _reports.Reload();
        _reports.Show();
        _reports.WindowState = FormWindowState.Normal;
        _reports.BringToFront();
        _reports.Activate();
    }

    private void ShowStatus()
    {
        _form.Show();
        _form.WindowState = FormWindowState.Normal;
        _form.BringToFront();
        _ = PollAsync();
    }

    private void Notify(string title, string body, ToolTipIcon icon)
    {
        _tray.BalloonTipTitle = title;
        _tray.BalloonTipText = Truncate(body, 220);
        _tray.BalloonTipIcon = icon;
        _tray.ShowBalloonTip(4000);
    }

    // NotifyIcon.Text throws above 63 chars.
    private static string Truncate(string s, int max = 63) =>
        s.Length <= max ? s : s[..(max - 1)] + "…";

    private static void OpenPath(string target)
    {
        try
        {
            Process.Start(new ProcessStartInfo(target) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            MessageBox.Show($"Could not open {target}:\n{ex.Message}", "AI Teacher Radar",
                MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
    }

    private void Exit()
    {
        _timer.Stop();
        _tray.Visible = false;
        _tray.Dispose();
        _form.Dispose();
        _reports.Dispose();
        ExitThread();
    }
}
