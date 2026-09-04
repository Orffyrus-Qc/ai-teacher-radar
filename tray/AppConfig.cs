using System.Text.Json;

namespace RadarTray;

/// <summary>
/// Settings, with defaults that work on any machine rather than this one.
///
/// The installed app lives under Program Files, which is read-only at runtime,
/// so anything writable — the config, the SQLite store, the briefs, the compose
/// env file — lives under %LOCALAPPDATA%\AITeacherRadar instead.
/// </summary>
public sealed class AppConfig
{
    public static string UserRoot { get; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "AITeacherRadar");

    /// <summary>
    /// The folder holding docker-compose.yml. Installed, that is the exe's own
    /// folder; in a dev checkout the exe sits in tray\publish, so walk up.
    /// </summary>
    public static string RadarRoot { get; } = FindRadarRoot();

    /// <summary>
    /// Where the briefs and SQLite store live. A writable radar root (a dev
    /// checkout) keeps them beside the source; an installed copy under Program
    /// Files cannot be written to, so those move to %LOCALAPPDATA%.
    /// </summary>
    private static string WritableRoot { get; } =
        IsWritable(RadarRoot) ? RadarRoot : UserRoot;

    private static string FindRadarRoot()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        for (var i = 0; i < 4 && dir is not null; i++)
        {
            if (File.Exists(Path.Combine(dir.FullName, "docker-compose.yml")))
                return dir.FullName;
            dir = dir.Parent;
        }
        return AppContext.BaseDirectory;
    }

    private static bool IsWritable(string dir)
    {
        try
        {
            var probe = Path.Combine(dir, $".write-probe-{Guid.NewGuid():N}");
            File.WriteAllText(probe, "");
            File.Delete(probe);
            return true;
        }
        catch
        {
            return false;
        }
    }

    public string ComposeFile { get; set; } =
        Path.Combine(RadarRoot, "docker-compose.yml");

    public string ProjectName { get; set; } = "ai-teacher-radar";

    /// <summary>
    /// Network of an existing n8n container. The n8n override is applied only
    /// when this network exists: an absent external network makes
    /// `docker compose up` fail outright.
    /// </summary>
    public string N8nNetwork { get; set; } = "n8n_default";
    public string ServiceName { get; set; } = "ai-teacher-radar";
    public string ContainerName { get; set; } = "ai-teacher-radar";
    public string BaseUrl { get; set; } = "http://127.0.0.1:8791";
    public string OutputFolder { get; set; } = Path.Combine(WritableRoot, "out");
    public string DataFolder { get; set; } = Path.Combine(WritableRoot, "data");
    public string DownloadsFolder { get; set; } = Path.Combine(WritableRoot, "data", "downloads");
    public string EnvFile { get; set; } = Path.Combine(WritableRoot, ".env");
    public int PollSeconds { get; set; } = 5;

    /// <summary>Last model picked for manual runs; null means the default.</summary>
    public string? PreferredModel { get; set; }

    /// <summary>
    /// Open the app window when the app starts. Off is useful when launching
    /// from the Startup folder, where a window on every login is noise -
    /// pass --minimized for that without changing this.
    /// </summary>
    public bool OpenWindowOnStartup { get; set; } = true;

    /// <summary>Notify on the transitions that actually matter, not every poll.</summary>
    public bool NotifyOnStateChange { get; set; } = true;

    /// <summary>
    /// Prefer a per-user config; fall back to one beside the exe so a dev
    /// checkout can still override things without touching the profile.
    /// </summary>
    private static string UserConfigPath => Path.Combine(UserRoot, "radartray.json");
    private static string LocalConfigPath => Path.Combine(AppContext.BaseDirectory, "radartray.json");

    public static AppConfig Load()
    {
        foreach (var path in new[] { UserConfigPath, LocalConfigPath })
        {
            try
            {
                if (!File.Exists(path)) continue;
                var cfg = JsonSerializer.Deserialize<AppConfig>(File.ReadAllText(path),
                    new JsonSerializerOptions { PropertyNameCaseInsensitive = true });
                if (cfg is not null) return cfg;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"{path} ignored: {ex.Message}");
            }
        }
        return new AppConfig();
    }

    public void Save()
    {
        try
        {
            Directory.CreateDirectory(UserRoot);
            File.WriteAllText(UserConfigPath,
                JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"could not save radartray.json: {ex.Message}");
        }
    }

    /// <summary>
    /// Make the writable side of the install exist before docker is invoked:
    /// the data/out folders, and an env file giving compose absolute paths plus
    /// this machine's timezone. Runs on every start; only writes what is absent.
    /// </summary>
    public void EnsureUserEnvironment()
    {
        Directory.CreateDirectory(Path.GetDirectoryName(EnvFile) ?? UserRoot);
        Directory.CreateDirectory(OutputFolder);
        Directory.CreateDirectory(DataFolder);
        Directory.CreateDirectory(DownloadsFolder);

        if (File.Exists(EnvFile))
        {
            EnsureEnvKey("RADAR_DOWNLOADS_DIR", DownloadsFolder.Replace('\\', '/'));
            var tokenFile = HuggingFaceTokenFile();
            if (tokenFile is not null)
                EnsureEnvKey("HF_TOKEN_FILE", tokenFile);
            return;
        }

        // Seed from the shipped .env.example so every tuning knob is present
        // and documented; then pin the machine-specific values.
        var template = Path.Combine(AppContext.BaseDirectory, ".env.example");
        var lines = File.Exists(template)
            ? File.ReadAllLines(template).ToList()
            : ["# generated by RadarTray"];

        void Set(string key, string value)
        {
            var i = lines.FindIndex(l => l.StartsWith(key + "=", StringComparison.Ordinal));
            if (i >= 0) lines[i] = $"{key}={value}";
            else lines.Add($"{key}={value}");
        }

        Set("TZ", IanaTimeZone());
        Set("RADAR_OUT_DIR", OutputFolder.Replace('\\', '/'));
        Set("RADAR_DATA_DIR", DataFolder.Replace('\\', '/'));
        Set("RADAR_DOWNLOADS_DIR", DownloadsFolder.Replace('\\', '/'));
        var hfTokenPath = HuggingFaceTokenFile();
        if (hfTokenPath is not null)
            Set("HF_TOKEN_FILE", hfTokenPath);

        File.WriteAllLines(EnvFile, lines);
    }

    /// <summary>Existing huggingface-cli login, if present. Never copies the secret.</summary>
    private static string? HuggingFaceTokenFile()
    {
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        var path = Path.Combine(home, ".cache", "huggingface", "token");
        return File.Exists(path) ? path.Replace('\\', '/') : null;
    }

    private void EnsureEnvKey(string key, string value)
    {
        try
        {
            var lines = File.ReadAllLines(EnvFile).ToList();
            var i = lines.FindIndex(l => l.StartsWith(key + "=", StringComparison.Ordinal));
            if (i >= 0)
            {
                var current = lines[i][(key.Length + 1)..].Trim();
                if (current.Length > 0) return;
                lines[i] = $"{key}={value}";
            }
            else lines.Add($"{key}={value}");
            File.WriteAllLines(EnvFile, lines);
        }
        catch
        {
            // Next Start can retry.
        }
    }

    /// <summary>
    /// The n8n network name, preferring N8N_NETWORK from the env file so this
    /// check agrees with what compose substitutes into the override. Without
    /// this the two could disagree and the override would be silently skipped.
    /// </summary>
    public string ResolveN8nNetwork()
    {
        try
        {
            if (File.Exists(EnvFile))
            {
                foreach (var line in File.ReadLines(EnvFile))
                {
                    var t = line.Trim();
                    if (t.StartsWith("N8N_NETWORK=", StringComparison.Ordinal))
                    {
                        var value = t["N8N_NETWORK=".Length..].Trim();
                        if (value.Length > 0) return value;
                    }
                }
            }
        }
        catch
        {
            // Fall back to the configured default.
        }
        return N8nNetwork;
    }

    /// <summary>The container is Linux, so it needs an IANA zone, not "Eastern Standard Time".</summary>
    private static string IanaTimeZone()
    {
        try
        {
            if (TimeZoneInfo.TryConvertWindowsIdToIanaId(TimeZoneInfo.Local.Id, out var iana))
                return iana;
        }
        catch
        {
            // Falls through to UTC.
        }
        return "UTC";
    }
}
