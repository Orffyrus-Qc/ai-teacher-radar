using System.Globalization;

namespace RadarTray;

public enum ReportKind { Search, Daily, Weekly, Deferred }

public sealed record ReportEntry(
    string Path,
    ReportKind Kind,
    DateTime Date,
    string Slot,
    string Title,
    int Items,
    DateTime Modified)
{
    public string KindLabel => Kind switch
    {
        ReportKind.Search => "Search",
        ReportKind.Daily => "Daily synthesis",
        ReportKind.Weekly => "Weekly synthesis",
        _ => "Deferred"
    };

    public string DateLabel => Kind == ReportKind.Weekly
        ? Title.Replace("Weekly synthesis ", "")
        : Date.ToString("yyyy-MM-dd");
}

/// <summary>
/// Scans the out/ tree. Everything the radar writes is Markdown with YAML front
/// matter, so the list columns come from the file itself rather than a sidecar
/// index that could drift.
/// </summary>
public static class Reports
{
    public static List<ReportEntry> Scan(string outFolder)
    {
        var found = new List<ReportEntry>();
        if (!Directory.Exists(outFolder)) return found;

        foreach (var file in Directory.EnumerateFiles(outFolder, "*.md", SearchOption.AllDirectories))
        {
            try
            {
                found.Add(Parse(file));
            }
            catch
            {
                // A half-written file mid-run must not break the list.
            }
        }

        // Newest first, and within a day put the synthesis above its searches.
        return found
            .OrderByDescending(r => r.Date)
            .ThenByDescending(r => r.Kind == ReportKind.Weekly)
            .ThenByDescending(r => r.Kind == ReportKind.Daily)
            .ThenByDescending(r => r.Slot)
            .ToList();
    }

    private static ReportEntry Parse(string path)
    {
        var name = System.IO.Path.GetFileName(path);
        var fm = FrontMatter(path);

        var kind = name.EndsWith(".DEFERRED.md", StringComparison.OrdinalIgnoreCase) ? ReportKind.Deferred
                 : name.Equals("SYNTHESIS.md", StringComparison.OrdinalIgnoreCase) ? ReportKind.Daily
                 : path.Contains($"{System.IO.Path.DirectorySeparatorChar}weekly{System.IO.Path.DirectorySeparatorChar}",
                                 StringComparison.OrdinalIgnoreCase) ? ReportKind.Weekly
                 : ReportKind.Search;

        var modified = File.GetLastWriteTime(path);

        // Prefer the folder name for the date; it is the run's local day.
        var folder = System.IO.Path.GetFileName(System.IO.Path.GetDirectoryName(path)) ?? "";
        if (!DateTime.TryParseExact(folder, "yyyy-MM-dd", CultureInfo.InvariantCulture,
                                    DateTimeStyles.None, out var date))
        {
            if (!fm.TryGetValue("date", out var d) ||
                !DateTime.TryParse(d, CultureInfo.InvariantCulture, DateTimeStyles.None, out date))
                date = modified.Date;
        }

        // Only a search brief is named HHMM-*; reading a time out of
        // "2026-W35.md" would turn the weekly into "20:26".
        var slot = fm.GetValueOrDefault("slot", "");
        if (slot.Length == 0 && kind is ReportKind.Search or ReportKind.Deferred
            && name.Length >= 4 && name[..4].All(char.IsDigit))
            slot = $"{name[..2]}:{name[2..4]}";
        if (slot.Length == 0)
            slot = kind switch
            {
                ReportKind.Weekly => System.IO.Path.GetFileNameWithoutExtension(path)
                                         .Split('-').LastOrDefault() ?? "week",
                ReportKind.Daily => "day",
                _ => "—"
            };

        var title = fm.GetValueOrDefault("title", System.IO.Path.GetFileNameWithoutExtension(path));
        _ = int.TryParse(fm.GetValueOrDefault("items", "0"), out var items);

        return new ReportEntry(path, kind, date, slot, title, items, modified);
    }

    private static Dictionary<string, string> FrontMatter(string path)
    {
        var map = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        using var reader = new StreamReader(path);

        if (reader.ReadLine() is not "---") return map;
        for (var i = 0; i < 40; i++)
        {
            var line = reader.ReadLine();
            if (line is null || line == "---") break;
            var split = line.IndexOf(':');
            if (split <= 0) continue;
            map[line[..split].Trim()] = line[(split + 1)..].Trim();
        }
        return map;
    }
}
