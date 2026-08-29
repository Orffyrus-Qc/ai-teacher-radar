using System.Diagnostics;

namespace RadarTray;

/// <summary>
/// Reader for everything the radar has written: every scheduled search brief,
/// the daily syntheses and the weekly ones, newest first.
/// </summary>
public sealed class ReportsForm : Form
{
    private static readonly Color Bg = Color.FromArgb(24, 26, 30);
    private static readonly Color Panel = Color.FromArgb(32, 35, 40);
    private static readonly Color Fg = Color.FromArgb(226, 232, 238);

    private readonly AppConfig _cfg;
    private readonly ListView _list = new();
    private readonly MarkdownView _view = new();
    private readonly TextBox _filter = new();
    private readonly Label _status = new();
    private readonly Label _state = new();
    private readonly FileSystemWatcher? _watcher;
    private readonly System.Windows.Forms.Timer _debounce = new() { Interval = 900 };

    private readonly SplitContainer _split;
    private List<ReportEntry> _all = [];
    private string? _current;

    public ReportsForm(AppConfig cfg)
    {
        _cfg = cfg;

        Text = "AI Teacher Radar — reports";
        Size = new Size(1120, 760);
        MinimumSize = new Size(760, 480);
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Bg;
        ForeColor = Fg;
        Font = new Font("Segoe UI", 9.5f);
        ShowInTaskbar = true;

        // ---- toolbar ----
        var bar = new Panel { Dock = DockStyle.Top, Height = 42, BackColor = Panel };

        _filter.SetBounds(10, 9, 260, 24);
        _filter.BackColor = Bg;
        _filter.ForeColor = Fg;
        _filter.BorderStyle = BorderStyle.FixedSingle;
        _filter.PlaceholderText = "Filter by date, kind or title…";
        _filter.TextChanged += (_, _) => Populate();

        var refresh = Button("Refresh", 280);
        refresh.Click += (_, _) => Reload();

        var folder = Button("Open folder", 366);
        folder.Click += (_, _) => Open(_cfg.OutputFolder);

        var editor = Button("Open in editor", 476);
        editor.Click += (_, _) => { if (_current is not null) Open(_current); };

        bar.Controls.AddRange([_filter, refresh, folder, editor]);

        // ---- split ----
        _split = new SplitContainer
        {
            Dock = DockStyle.Fill,
            BackColor = Color.FromArgb(48, 53, 60),
            FixedPanel = FixedPanel.Panel1
        };

        _list.Dock = DockStyle.Fill;
        _list.View = View.Details;
        _list.FullRowSelect = true;
        _list.MultiSelect = false;
        _list.HideSelection = false;
        _list.BackColor = Panel;
        _list.ForeColor = Fg;
        _list.BorderStyle = BorderStyle.None;
        _list.Columns.Add("When", 90);
        _list.Columns.Add("Kind", 150);
        _list.Columns.Add("Items", 60, HorizontalAlignment.Right);
        _list.SelectedIndexChanged += (_, _) => ShowSelected();
        _list.DoubleClick += (_, _) => { if (_current is not null) Open(_current); };

        _view.Dock = DockStyle.Fill;
        _view.Font = new Font("Segoe UI", 9.5f);
        _view.ApplyTheme(Bg, Fg);

        _split.Panel1.Controls.Add(_list);
        _split.Panel2.Controls.Add(_view);

        // One strip at the bottom: what you are reading on the left, what the
        // radar is doing on the right, so this window alone tells you both.
        var strip = new Panel { Dock = DockStyle.Bottom, Height = 22, BackColor = Bg };

        _state.Dock = DockStyle.Right;
        _state.Width = 260;
        _state.TextAlign = ContentAlignment.MiddleRight;
        _state.ForeColor = Color.FromArgb(150, 160, 172);
        _state.Padding = new Padding(0, 0, 12, 0);
        _state.Text = "connecting…";

        _status.Dock = DockStyle.Fill;
        _status.TextAlign = ContentAlignment.MiddleLeft;
        _status.ForeColor = Color.FromArgb(150, 160, 172);
        _status.Padding = new Padding(10, 0, 0, 0);

        strip.Controls.Add(_status);
        strip.Controls.Add(_state);

        Controls.AddRange([_split, strip, bar]);

        // A run finishing mid-read should show up without a manual refresh.
        try
        {
            if (Directory.Exists(_cfg.OutputFolder))
            {
                _watcher = new FileSystemWatcher(_cfg.OutputFolder, "*.md")
                {
                    IncludeSubdirectories = true,
                    EnableRaisingEvents = true,
                    NotifyFilter = NotifyFilters.FileName | NotifyFilters.LastWrite | NotifyFilters.Size
                };
                FileSystemEventHandler bump = (_, _) => BeginInvoke(() => { _debounce.Stop(); _debounce.Start(); });
                _watcher.Created += bump;
                _watcher.Changed += bump;
                _watcher.Deleted += bump;
            }
        }
        catch
        {
            // Watching is a convenience; the Refresh button is the guarantee.
        }
        _debounce.Tick += (_, _) => { _debounce.Stop(); Reload(); };

        FormClosing += (_, e) =>
        {
            if (e.CloseReason != CloseReason.UserClosing) return;
            e.Cancel = true;
            Hide();
        };

        Reload();
    }

    protected override void OnShown(EventArgs e)
    {
        base.OnShown(e);
        // Set once the form has its real width, or the value is discarded.
        var wanted = 330;
        var max = Math.Max(_split.Panel1MinSize + 1, _split.Width - _split.Panel2MinSize - 6);
        _split.SplitterDistance = Math.Clamp(wanted, _split.Panel1MinSize + 1, max);
    }

    private static Button Button(string text, int x)
    {
        var b = new Button
        {
            Text = text,
            Bounds = new Rectangle(x, 8, text.Length * 8 + 26, 26),
            FlatStyle = FlatStyle.Flat,
            BackColor = Color.FromArgb(46, 51, 58),
            ForeColor = Fg
        };
        b.FlatAppearance.BorderColor = Color.FromArgb(70, 78, 88);
        return b;
    }

    /// <summary>Live radar state, pushed in by the tray on each poll.</summary>
    public void SetState(string headline, Color colour)
    {
        _state.Text = headline;
        _state.ForeColor = colour;
    }

    public void Reload()
    {
        var keep = _current;
        _all = Reports.Scan(_cfg.OutputFolder);
        Populate();

        if (keep is not null)
        {
            foreach (ListViewItem row in _list.Items)
                if (((ReportEntry)row.Tag!).Path == keep) { row.Selected = true; return; }
        }
        if (_list.Items.Count > 0 && _list.SelectedItems.Count == 0)
            _list.Items[0].Selected = true;
    }

    private void Populate()
    {
        var needle = _filter.Text.Trim();
        var shown = _all.Where(r =>
            needle.Length == 0 ||
            r.Title.Contains(needle, StringComparison.OrdinalIgnoreCase) ||
            r.KindLabel.Contains(needle, StringComparison.OrdinalIgnoreCase) ||
            r.DateLabel.Contains(needle, StringComparison.OrdinalIgnoreCase)).ToList();

        _list.BeginUpdate();
        _list.Items.Clear();
        _list.Groups.Clear();

        var groups = new Dictionary<string, ListViewGroup>();
        foreach (var r in shown)
        {
            if (!groups.TryGetValue(r.DateLabel, out var group))
            {
                group = new ListViewGroup(r.DateLabel) { HeaderAlignment = HorizontalAlignment.Left };
                groups[r.DateLabel] = group;
                _list.Groups.Add(group);
            }

            var row = new ListViewItem([r.Slot, r.KindLabel, r.Items > 0 ? r.Items.ToString() : "—"])
            {
                Tag = r,
                Group = group,
                ForeColor = r.Kind switch
                {
                    ReportKind.Daily => Color.FromArgb(120, 200, 255),
                    ReportKind.Weekly => Color.FromArgb(170, 160, 255),
                    ReportKind.Deferred => Color.FromArgb(243, 180, 90),
                    _ => Fg
                }
            };
            _list.Items.Add(row);
        }
        _list.EndUpdate();

        _status.Text = shown.Count == _all.Count
            ? $"{_all.Count} report(s)"
            : $"{shown.Count} of {_all.Count} report(s)";
    }

    private void ShowSelected()
    {
        if (_list.SelectedItems.Count == 0) return;
        var entry = (ReportEntry)_list.SelectedItems[0].Tag!;
        _current = entry.Path;
        _view.RenderFile(entry.Path);
        _status.Text = $"{entry.Path}   ·   written {entry.Modified:ddd dd MMM HH:mm}";
    }

    private static void Open(string target)
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

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _watcher?.Dispose();
            _debounce.Dispose();
        }
        base.Dispose(disposing);
    }
}
