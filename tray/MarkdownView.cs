using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;

namespace RadarTray;

/// <summary>
/// A read-only RichTextBox that renders the subset of Markdown the radar
/// actually emits: headings, bold/italic/code, bullets, links, tables, rules
/// and the &lt;sub&gt; footer. Deliberately not a general Markdown engine — a
/// full one would mean a NuGet dependency and a browser control, and this app
/// is meant to stay a single dependency-free exe.
/// </summary>
public sealed class MarkdownView : RichTextBox
{
    private const int WM_SETREDRAW = 0x000B;
    private const int EM_SETRECT = 0x00B3;
    private const int MaxCell = 30;
    private const int MaxModelCell = 96;

    [DllImport("user32.dll")]
    private static extern int SendMessage(IntPtr hWnd, int msg, bool wParam, int lParam);

    [DllImport("user32.dll")]
    private static extern int SendMessage(IntPtr hWnd, int msg, int wParam, ref Rect rect);

    [StructLayout(LayoutKind.Sequential)]
    private struct Rect { public int Left, Top, Right, Bottom; }

    /// <summary>
    /// Breathing room around the text. Applied as the rich-edit formatting
    /// rectangle rather than control Padding, so the text insets but the
    /// scrollbar stays flush against the pane edge. A field, not a property:
    /// a public Padding property trips the WinForms designer-serialization
    /// analyzer (WFO1000) and this is never touched by a designer.
    /// </summary>
    public Padding TextMargin = new(18, 14, 18, 14);

    private void ApplyTextMargin()
    {
        if (!IsHandleCreated) return;
        var r = new Rect
        {
            Left = TextMargin.Left,
            Top = TextMargin.Top,
            Right = Math.Max(TextMargin.Left + 40, ClientSize.Width - TextMargin.Right),
            // Full height on purpose. Insetting the bottom makes RichEdit clip
            // the last line mid-way and orphan its bullet marker; the bottom is
            // a scroll boundary, so padding comes from trailing blank lines at
            // the end of the document instead.
            Bottom = ClientSize.Height
        };
        SendMessage(Handle, EM_SETRECT, 0, ref r);
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        ApplyTextMargin();
    }

    protected override void OnSizeChanged(EventArgs e)
    {
        base.OnSizeChanged(e);
        ApplyTextMargin();
    }

    private static readonly Regex Inline = new(
        @"(\[(?<txt>[^\]]+)\]\((?<url>[^)]+)\))" +
        @"|(\*\*(?<b>[^*]+)\*\*)" +
        @"|(`(?<c>[^`]+)`)" +
        @"|(\*(?<i>[^*]+)\*)",
        RegexOptions.Compiled);

    private readonly List<(int Start, int Length, string Url)> _links = [];

    private Font _body = null!, _bodyBold = null!, _bodyItalic = null!, _mono = null!;
    private Font _h1 = null!, _h2 = null!, _h3 = null!, _small = null!;

    private Color _fg, _muted, _link, _codeFg, _headFg;

    public MarkdownView()
    {
        ReadOnly = true;
        BorderStyle = BorderStyle.None;
        DetectUrls = false;          // markdown links are handled explicitly
        WordWrap = true;
        ScrollBars = RichTextBoxScrollBars.Vertical;

        MouseMove += (_, e) => Cursor = LinkAt(e.Location) is null ? Cursors.Default : Cursors.Hand;
        MouseUp += (_, e) =>
        {
            if (e.Button != MouseButtons.Left) return;
            var url = LinkAt(e.Location);
            if (url is null) return;
            try
            {
                Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
            }
            catch (Exception ex)
            {
                MessageBox.Show($"Could not open {url}:\n{ex.Message}", "AI Teacher Radar",
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        };
    }

    private void BuildFonts()
    {
        var size = Font.Size;
        _body = new Font("Segoe UI", size);
        _bodyBold = new Font("Segoe UI", size, FontStyle.Bold);
        _bodyItalic = new Font("Segoe UI", size, FontStyle.Italic);
        _mono = new Font("Consolas", size - 0.5f);
        _h1 = new Font("Segoe UI Semibold", size + 6f, FontStyle.Bold);
        _h2 = new Font("Segoe UI Semibold", size + 3f, FontStyle.Bold);
        _h3 = new Font("Segoe UI Semibold", size + 1f, FontStyle.Bold);
        _small = new Font("Segoe UI", size - 1f, FontStyle.Italic);
    }

    public void ApplyTheme(Color background, Color foreground)
    {
        BackColor = background;
        _fg = foreground;
        _muted = Color.FromArgb(150, 160, 172);
        _link = Color.FromArgb(94, 168, 240);
        _codeFg = Color.FromArgb(236, 178, 118);
        _headFg = Color.FromArgb(240, 244, 248);
        ForeColor = _fg;
    }

    private string? LinkAt(Point p)
    {
        var index = GetCharIndexFromPosition(p);
        foreach (var (start, length, url) in _links)
            if (index >= start && index < start + length)
                return url;
        return null;
    }

    public void RenderFile(string path)
    {
        string text;
        try
        {
            text = File.ReadAllText(path);
        }
        catch (Exception ex)
        {
            Render($"# Could not read the file\n\n{ex.Message}");
            return;
        }
        Render(text);
    }

    public void Render(string markdown)
    {
        BuildFonts();
        SendMessage(Handle, WM_SETREDRAW, false, 0);
        try
        {
            Clear();
            _links.Clear();

            var lines = markdown.Replace("\r\n", "\n").Split('\n');
            var i = 0;

            // Front matter is metadata for Obsidian, not for a reader.
            if (lines.Length > 0 && lines[0].Trim() == "---")
            {
                i = 1;
                while (i < lines.Length && lines[i].Trim() != "---") i++;
                i++;
            }

            for (; i < lines.Length; i++)
            {
                var line = lines[i];

                if (line.StartsWith('|'))
                {
                    var table = new List<string>();
                    while (i < lines.Length && lines[i].StartsWith('|')) table.Add(lines[i++]);
                    i--;
                    RenderTable(table);
                    continue;
                }

                RenderLine(line);
            }

        }
        finally
        {
            SendMessage(Handle, WM_SETREDRAW, true, 0);
            // Both of these must happen with drawing switched back on: a
            // ScrollToCaret issued while WM_SETREDRAW is false leaves the
            // control on a stale offset, so the report opens mid-document.
            ApplyTextMargin();
            SelectionStart = 0;
            SelectionLength = 0;
            ScrollToCaret();
            // Refresh, not Invalidate: a queued partial repaint after
            // WM_SETREDRAW was suppressed leaves bands of the previous
            // document still on screen.
            Refresh();
        }
    }

    private void RenderLine(string line)
    {
        var trimmed = line.TrimStart();

        if (trimmed.Length == 0) { Append("\n", _body, _fg); return; }

        if (trimmed is "---" or "***" or "___")
        {
            Append(new string('─', 60) + "\n", _body, Color.FromArgb(70, 78, 88));
            return;
        }

        if (trimmed.StartsWith("### ")) { Append("\n" + trimmed[4..] + "\n", _h3, _headFg); return; }
        if (trimmed.StartsWith("## ")) { Append("\n" + trimmed[3..] + "\n", _h2, _headFg); return; }
        if (trimmed.StartsWith("# ")) { Append(trimmed[2..] + "\n", _h1, _headFg); return; }

        if (trimmed.StartsWith("<sub>"))
        {
            var inner = trimmed.Replace("<sub>", "").Replace("</sub>", "");
            AppendInline(inner + "\n", _small, _muted);
            return;
        }

        if (trimmed.StartsWith("> "))
        {
            Append("  ▏", _body, Color.FromArgb(90, 100, 112));
            AppendInline(trimmed[2..] + "\n", _bodyItalic, _muted);
            return;
        }

        // Bullets keep their original indentation depth.
        if (trimmed.StartsWith("- ") || trimmed.StartsWith("* "))
        {
            var depth = (line.Length - trimmed.Length) / 2;
            Append(new string(' ', depth * 4) + "•  ", _body, Color.FromArgb(120, 190, 255));
            AppendInline(trimmed[2..] + "\n", _body, _fg);
            return;
        }

        // Continuation lines of a bullet (the radar indents these by two spaces).
        if (line.StartsWith("  ") && !line.StartsWith("    "))
        {
            Append("     ", _body, _fg);
            AppendInline(trimmed + "\n", _body, _muted);
            return;
        }

        AppendInline(line + "\n", _body, _fg);
    }

    private void RenderTable(List<string> rows)
    {
        var grid = rows
            .Select(r => r.Trim().Trim('|').Split('|').Select(c => c.Trim()).ToArray())
            .Where(cells => !cells.All(c => c.Length == 0 || c.All(ch => ch is '-' or ':')))
            .ToList();
        if (grid.Count == 0) return;

        var columns = grid.Max(r => r.Length);
        var modelCol = -1;
        if (grid.Count > 0)
        {
            for (var c = 0; c < grid[0].Length; c++)
            {
                var header = Strip(grid[0][c]);
                if (header.Equals("Model", StringComparison.OrdinalIgnoreCase)
                    || header.StartsWith("Model ", StringComparison.OrdinalIgnoreCase))
                    modelCol = c;
            }
        }
        var widths = new int[columns];
        for (var c = 0; c < columns; c++)
        {
            // Model names (HF ids) need the full string; other columns stay
            // capped so Params/Fitness/License do not inflate the row.
            var cap = c == modelCol ? MaxModelCell : MaxCell;
            widths[c] = Math.Min(cap,
                grid.Max(r => c < r.Length ? Strip(r[c]).Length : 0));
        }

        Append("\n", _body, _fg);
        for (var r = 0; r < grid.Count; r++)
        {
            var row = grid[r];
            var font = r == 0 ? new Font(_mono, FontStyle.Bold) : _mono;
            var line = new StringBuilder();
            for (var c = 0; c < columns; c++)
            {
                var cell = c < row.Length ? Strip(row[c]) : "";
                if (cell.Length > widths[c]) cell = cell[..(widths[c] - 1)] + "…";
                line.Append(cell.PadRight(widths[c] + 2));
            }
            Append(line.ToString().TrimEnd() + "\n", font, r == 0 ? _headFg : _fg);
        }
        Append("\n", _body, _fg);
    }

    /// <summary>Tables are laid out monospaced, so inline markup is flattened.</summary>
    private static string Strip(string cell) =>
        Inline.Replace(cell, m =>
            m.Groups["txt"].Success ? m.Groups["txt"].Value
            : m.Groups["b"].Success ? m.Groups["b"].Value
            : m.Groups["c"].Success ? m.Groups["c"].Value
            : m.Groups["i"].Value);

    private void AppendInline(string text, Font font, Color colour)
    {
        var pos = 0;
        foreach (Match m in Inline.Matches(text))
        {
            if (m.Index > pos) Append(text[pos..m.Index], font, colour);

            if (m.Groups["txt"].Success)
                Append(m.Groups["txt"].Value, font, _link, m.Groups["url"].Value);
            else if (m.Groups["b"].Success)
                Append(m.Groups["b"].Value, new Font(font, FontStyle.Bold), colour);
            else if (m.Groups["c"].Success)
                Append(m.Groups["c"].Value, _mono, _codeFg);
            else
                Append(m.Groups["i"].Value, new Font(font, FontStyle.Italic), _muted);

            pos = m.Index + m.Length;
        }
        if (pos < text.Length) Append(text[pos..], font, colour);
    }

    private void Append(string text, Font font, Color colour, string? url = null)
    {
        var start = TextLength;
        SelectionStart = start;
        SelectionLength = 0;
        SelectionFont = font;
        SelectionColor = colour;
        AppendText(text);
        if (url is not null) _links.Add((start, text.Length, url));
    }
}
