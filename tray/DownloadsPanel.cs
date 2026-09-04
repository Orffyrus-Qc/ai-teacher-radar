using System.Diagnostics;

namespace RadarTray;

/// <summary>List of leak/drop downloads with an open-folder action.</summary>
internal sealed class DownloadsPanel : UserControl
{
    private static readonly Color Panel = Color.FromArgb(32, 35, 40);
    private static readonly Color Fg = Color.FromArgb(226, 232, 238);

    private readonly AppConfig _cfg;
    private readonly ListView _list = new();
    private readonly Label _status = new();
    private IReadOnlyList<DownloadRow> _rows = [];
    private string _folder;

    public DownloadsPanel(AppConfig cfg)
    {
        _cfg = cfg;
        _folder = cfg.DownloadsFolder;
        Dock = DockStyle.Fill;
        BackColor = Panel;

        var bar = new Panel { Dock = DockStyle.Top, Height = 36, BackColor = Panel };
        var folderBtn = new Button
        {
            Bounds = new Rectangle(8, 5, 28, 26),
            FlatStyle = FlatStyle.Flat,
            Image = FolderGlyph.Small(),
            ImageAlign = ContentAlignment.MiddleCenter,
            BackColor = Color.FromArgb(46, 51, 58),
            ForeColor = Fg,
        };
        folderBtn.FlatAppearance.BorderColor = Color.FromArgb(70, 78, 88);
        new ToolTip().SetToolTip(folderBtn, "Open downloads folder");
        folderBtn.Click += (_, _) => Open(_folder);

        var openSel = new Button
        {
            Text = "Open selected folder",
            Bounds = new Rectangle(42, 5, 150, 26),
            FlatStyle = FlatStyle.Flat,
            BackColor = Color.FromArgb(46, 51, 58),
            ForeColor = Fg,
        };
        openSel.FlatAppearance.BorderColor = Color.FromArgb(70, 78, 88);
        openSel.Click += (_, _) => OpenSelected();

        bar.Controls.Add(folderBtn);
        bar.Controls.Add(openSel);

        _list.Dock = DockStyle.Fill;
        _list.View = View.Details;
        _list.FullRowSelect = true;
        _list.MultiSelect = false;
        _list.HideSelection = false;
        _list.BackColor = Panel;
        _list.ForeColor = Fg;
        _list.BorderStyle = BorderStyle.None;
        _list.Columns.Add("Status", 90);
        _list.Columns.Add("Model", 280);
        _list.Columns.Add("License", 90);
        _list.Columns.Add("Size", 80, HorizontalAlignment.Right);
        _list.Columns.Add("Note", 280);
        _list.DoubleClick += (_, _) => OpenSelected();

        _status.Dock = DockStyle.Bottom;
        _status.Height = 22;
        _status.ForeColor = Color.FromArgb(150, 160, 172);
        _status.TextAlign = ContentAlignment.MiddleLeft;
        _status.Padding = new Padding(8, 0, 0, 0);

        Controls.Add(_list);
        Controls.Add(_status);
        Controls.Add(bar);
    }

    public void Bind(IReadOnlyList<DownloadRow> rows, string folder, bool hfAuth = false)
    {
        _rows = rows;
        if (!string.IsNullOrWhiteSpace(folder))
            _folder = folder.Contains("/app/") ? _cfg.DownloadsFolder : folder;
        _list.BeginUpdate();
        _list.Items.Clear();
        foreach (var row in rows)
        {
            var item = new ListViewItem([
                row.Status,
                row.ModelId ?? row.Title,
                row.LicenseId ?? "—",
                FormatBytes(row.Bytes),
                row.Reason ?? "",
            ])
            {
                Tag = row,
                ForeColor = row.Status switch
                {
                    "done" => Color.FromArgb(46, 204, 113),
                    "downloading" or "queued" => Color.FromArgb(52, 152, 219),
                    "failed" or "refused" => Color.FromArgb(231, 76, 60),
                    _ => Fg
                }
            };
            _list.Items.Add(item);
        }
        _list.EndUpdate();
        var account = hfAuth ? "HF account: signed in" : "HF account: not signed in";
        _status.Text = rows.Count == 0
            ? $"No leak/drop downloads yet.  {account}"
            : $"{rows.Count} download(s)  ·  {account}  ·  {_folder}";
    }

    private void OpenSelected()
    {
        if (_list.SelectedItems.Count == 0)
        {
            Open(_folder);
            return;
        }
        var row = (DownloadRow)_list.SelectedItems[0].Tag!;
        if (!string.IsNullOrWhiteSpace(row.Path) && File.Exists(row.Path))
            Open(Path.GetDirectoryName(row.Path)!);
        else if (!string.IsNullOrWhiteSpace(row.Path) && Directory.Exists(row.Path))
            Open(row.Path);
        else
            Open(_folder);
    }

    private static string FormatBytes(long? bytes)
    {
        if (bytes is null or <= 0) return "—";
        var n = (double)bytes.Value;
        string[] units = ["B", "KB", "MB", "GB"];
        var i = 0;
        while (n >= 1024 && i < units.Length - 1) { n /= 1024; i++; }
        return $"{n:0.#} {units[i]}";
    }

    private static void Open(string target)
    {
        try
        {
            Directory.CreateDirectory(target);
            Process.Start(new ProcessStartInfo(target) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            MessageBox.Show($"Could not open {target}:\n{ex.Message}", "AI Teacher Radar",
                MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
    }
}
