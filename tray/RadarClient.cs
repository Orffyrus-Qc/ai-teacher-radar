using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace RadarTray;

public enum RadarState
{
    Unknown,        // never polled
    Stopped,        // no container
    Starting,       // container up, API not answering yet
    Unhealthy,      // container up, API erroring
    Idle,           // alive, nothing queued
    Working,        // a job is harvesting / publishing
    DeferredGpu     // a job is waiting for the GPU
}

/// <summary>One poll's worth of truth about the radar.</summary>
public sealed record RadarStatus(
    RadarState State,
    string ContainerStatus,
    string WorkerPhase,
    string? CurrentJob,
    int Waiting,
    int DeferredByGpu,
    bool GpuBusy,
    string GpuSummary,
    string SchedulerMode,
    string LlmDetail,
    bool LlmReady,
    IReadOnlyList<(string Id, string NextRun)> Upcoming,
    IReadOnlyList<GpuInfo> Gpus,
    string? Error,
    double? RemainingSeconds = null,
    string EtaBasis = "",
    double? QueueRemainingSeconds = null,
    int QueueJobs = 0)
{
    public string Headline => State switch
    {
        RadarState.Stopped => "Stopped",
        RadarState.Starting => "Starting…",
        RadarState.Unhealthy => "Unhealthy",
        RadarState.Idle => "Alive — idle",
        RadarState.Working => $"Running — {WorkerPhase}{EtaSuffix}",
        RadarState.DeferredGpu => "Deferred — GPU busy",
        _ => "Unknown"
    };

    /// <summary>Only shown once there is history to average; never a guess.</summary>
    private string EtaSuffix =>
        RemainingSeconds is null ? "" : $" · ~{HumanDuration(RemainingSeconds.Value)} left";

    /// <summary>
    /// Everything outstanding, not just the job in flight. Only worth showing
    /// when more than one job is left, otherwise it just repeats the per-job
    /// figure.
    /// </summary>
    public string? QueueEta =>
        QueueJobs > 1 && QueueRemainingSeconds is not null
            ? $"{QueueJobs} jobs · ~{HumanDuration(QueueRemainingSeconds.Value)} total"
            : null;

    /// <summary>Headline plus the queue total, for the reader's status strip.</summary>
    public string StateLine =>
        QueueEta is null ? Headline : $"{Headline}  ·  {QueueEta}";

    /// <summary>How the estimate was reached, for the status window.</summary>
    public string EtaDetail =>
        RemainingSeconds is null
            ? (EtaBasis.Length > 0 ? EtaBasis : "not estimated")
            : $"~{HumanDuration(RemainingSeconds.Value)} remaining — {EtaBasis}";

    public static string HumanDuration(double seconds)
    {
        var s = (int)Math.Round(Math.Max(0, seconds));
        if (s < 60) return $"{s}s";
        if (s < 3600) return $"{s / 60}m {s % 60:00}s";
        return $"{s / 3600}h {(s % 3600) / 60:00}m";
    }
}

public sealed record GpuInfo(int Index, string Name, int UtilPct, int VramUsedMib, int VramTotalMib);

public sealed class DownloadRow
{
    public string Id { get; set; } = "";
    public string Title { get; set; } = "";
    public string Url { get; set; } = "";
    public string? ModelId { get; set; }
    public string? LicenseId { get; set; }
    public string Status { get; set; } = "";
    public string? Reason { get; set; }
    public string? Path { get; set; }
    public long? Bytes { get; set; }
    public string? FileName { get; set; }
    public string? UpdatedAt { get; set; }
}

public sealed record DownloadsSnapshot(
    IReadOnlyList<DownloadRow> Rows, string Folder, bool HfAuth);

public sealed class RadarClient(AppConfig cfg)
{
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(6) };

    // ----------------------------------------------------------- docker
    // --build so a freshly installed machine, with no image yet, comes up on
    // the first click of Start instead of failing.
    public async Task<(bool Ok, string Output)> StartAsync()
    {
        cfg.EnsureUserEnvironment();
        if (!await DockerReadyAsync())
        {
            await RunAsync("docker", "desktop start", TimeSpan.FromMinutes(2));
            await WaitDockerAsync(TimeSpan.FromSeconds(90));
        }
        if (await DockerReadyAsync())
            return await ComposeAsync("up -d --build");
        return await StartLocalAsync();
    }

    public async Task<(bool Ok, string Output)> StopAsync()
    {
        StopLocal();
        if (await DockerReadyAsync())
            return await ComposeAsync("down");
        return (true, "local radar stopped");
    }

    public async Task<(bool Ok, string Output)> RestartAsync()
    {
        var (ok, output) = await ComposeAsync("restart");
        return (ok, output);
    }

    private async Task<bool> DockerReadyAsync()
    {
        var (ok, _) = await RunAsync("docker", "info", TimeSpan.FromSeconds(8));
        return ok;
    }

    private async Task<bool> WaitDockerAsync(TimeSpan limit)
    {
        var until = DateTime.UtcNow + limit;
        while (DateTime.UtcNow < until)
        {
            if (await DockerReadyAsync()) return true;
            await Task.Delay(4000);
        }
        return false;
    }

    private string PidPath => Path.Combine(cfg.DataFolder, "radar-local.pid");

    private async Task<(bool Ok, string Output)> StartLocalAsync()
    {
        var scripts = Path.Combine(AppConfig.RadarRoot, ".venv", "Scripts");
        var pyw = Path.Combine(scripts, "pythonw.exe");
        var py = File.Exists(pyw) ? pyw : Path.Combine(scripts, "python.exe");
        if (!File.Exists(py))
            return (false,
                "Docker Desktop is not running, and there is no local .venv. " +
                "Start Docker Desktop, wait until it is ready, then click Start again. " +
                "Or create " + Path.Combine(AppConfig.RadarRoot, ".venv") + " and pip install -r requirements.txt.");

        StopLocal();
        try
        {
            var psi = new ProcessStartInfo(py,
                "-m uvicorn app.main:api --host 127.0.0.1 --port 8791 --log-level warning")
            {
                WorkingDirectory = AppConfig.RadarRoot,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                RedirectStandardInput = true,
            };
            ApplyDotEnv(psi, cfg.EnvFile);
            psi.Environment["PYTHONPATH"] = AppConfig.RadarRoot;
            var p = Process.Start(psi);
            if (p is null) return (false, "could not start local python radar");
            Directory.CreateDirectory(cfg.DataFolder);
            await File.WriteAllTextAsync(PidPath, p.Id.ToString());
            for (var i = 0; i < 20; i++)
            {
                await Task.Delay(500);
                try
                {
                    using var health = await GetJsonAsync("/health");
                    return (true, "Radar running locally (Docker Desktop was not available).");
                }
                catch
                {
                    if (p.HasExited)
                        return (false, "local radar exited during start");
                }
            }
            return (true, "Radar starting locally. If the icon stays grey, wait a few seconds.");
        }
        catch (Exception ex)
        {
            return (false, "Docker Desktop is not running. " + ex.Message);
        }
    }

    private void StopLocal()
    {
        try
        {
            if (!File.Exists(PidPath)) return;
            if (int.TryParse(File.ReadAllText(PidPath).Trim(), out var pid))
            {
                try
                {
                    using var p = Process.GetProcessById(pid);
                    p.Kill(entireProcessTree: true);
                }
                catch (ArgumentException) { /* already gone */ }
            }
            File.Delete(PidPath);
        }
        catch
        {
            // Best-effort.
        }
    }

    private static void ApplyDotEnv(ProcessStartInfo psi, string envFile)
    {
        if (!File.Exists(envFile)) return;
        foreach (var raw in File.ReadLines(envFile))
        {
            var line = raw.Trim();
            if (line.Length == 0 || line.StartsWith('#') || !line.Contains('=')) continue;
            var i = line.IndexOf('=');
            var key = line[..i].Trim();
            var value = line[(i + 1)..].Trim();
            if (key.Length > 0) psi.Environment[key] = value;
        }
    }

    private async Task<(bool Ok, string Output)> ComposeAsync(string args)
    {
        cfg.EnsureUserEnvironment();

        var files = $"-f \"{cfg.ComposeFile}\"";
        // Join n8n's network only if it is really there. Declaring an external
        // network that does not exist makes `up` fail with "declared as
        // external, but could not be found", which would brick a fresh install
        // on any machine that has no n8n.
        var dir = Path.GetDirectoryName(cfg.ComposeFile);
        var n8nOverride = dir is null ? null : Path.Combine(dir, "docker-compose.n8n.yml");
        if (n8nOverride is not null && File.Exists(n8nOverride)
            && await NetworkExistsAsync(cfg.ResolveN8nNetwork()))
            files += $" -f \"{n8nOverride}\"";

        var envFile = File.Exists(cfg.EnvFile) ? $"--env-file \"{cfg.EnvFile}\" " : "";
        // A fixed project name keeps the container identity stable no matter
        // where the compose file was installed.
        return await RunAsync("docker",
            $"compose {files} {envFile}-p {cfg.ProjectName} {args}",
            TimeSpan.FromMinutes(10));
    }

    private static async Task<bool> NetworkExistsAsync(string name)
    {
        if (string.IsNullOrWhiteSpace(name)) return false;
        var (ok, _) = await RunAsync("docker", $"network inspect {name}", TimeSpan.FromSeconds(15));
        return ok;
    }

    private async Task<string> ContainerStatusAsync()
    {
        var (ok, output) = await RunAsync("docker",
            $"ps -a --filter name=^/{cfg.ContainerName}$ --format {{{{.Status}}}}",
            TimeSpan.FromSeconds(15));
        return ok ? output.Trim() : "";
    }

    private static async Task<(bool Ok, string Output)> RunAsync(string exe, string args, TimeSpan timeout)
    {
        try
        {
            var psi = new ProcessStartInfo(exe, args)
            {
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                RedirectStandardInput = true,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };
            using var p = Process.Start(psi);
            if (p is null) return (false, $"could not start {exe}");

            var stdout = p.StandardOutput.ReadToEndAsync();
            var stderr = p.StandardError.ReadToEndAsync();
            using var cts = new CancellationTokenSource(timeout);
            await p.WaitForExitAsync(cts.Token);

            var text = (await stdout + Environment.NewLine + await stderr).Trim();
            return (p.ExitCode == 0, text);
        }
        catch (OperationCanceledException)
        {
            return (false, $"{exe} timed out");
        }
        catch (Exception ex)
        {
            return (false, ex.Message);
        }
    }

    // ------------------------------------------------------------- api
    /// <summary>Models Ollama has, plus the one a job gets if none is picked.</summary>
    public async Task<(IReadOnlyList<string> Models, string? Default)> GetModelsAsync()
    {
        try
        {
            using var doc = await GetJsonAsync("/models");
            var names = new List<string>();
            foreach (var m in doc.RootElement.GetProperty("models").EnumerateArray())
                if (m.TryGetProperty("name", out var n) && n.ValueKind == JsonValueKind.String)
                    names.Add(n.GetString()!);
            string? fallback = null;
            if (doc.RootElement.TryGetProperty("defaults", out var d)
                && d.TryGetProperty("notes", out var notes)
                && notes.ValueKind == JsonValueKind.String)
                fallback = notes.GetString();
            return (names, fallback);
        }
        catch
        {
            // Ollama down or an older container: the picker just stays empty.
            return (Array.Empty<string>(), null);
        }
    }

    public async Task<(bool Ok, string Body)> PostJobAsync(string kind, string? model = null)
    {
        try
        {
            var body = model is null
                ? $"{{\"kind\":\"{kind}\"}}"
                : $"{{\"kind\":\"{kind}\",\"model\":{JsonSerializer.Serialize(model)}}}";
            var content = new StringContent(body, Encoding.UTF8, "application/json");
            using var req = new HttpRequestMessage(HttpMethod.Post, $"{cfg.BaseUrl}/jobs") { Content = content };
            req.Headers.Add("X-Radar-Origin", "manual");
            var resp = await _http.SendAsync(req);
            return (resp.IsSuccessStatusCode, await resp.Content.ReadAsStringAsync());
        }
        catch (Exception ex)
        {
            return (false, ex.Message);
        }
    }

    public async Task<RadarStatus> PollAsync(bool includeGpuDetail)
    {
        var container = await ContainerStatusAsync();
        var running = container.StartsWith("Up", StringComparison.OrdinalIgnoreCase);

        JsonDocument? health = null, queue = null, gpu = null;
        string? error = null;
        try
        {
            health = await GetJsonAsync("/health");
            queue = await GetJsonAsync("/queue");
            if (includeGpuDetail) gpu = await GetJsonAsync("/gpu");
        }
        catch (Exception ex)
        {
            error = ex.Message;
        }

        if (health is null || queue is null)
        {
            if (string.IsNullOrEmpty(container) || !running)
                return Empty(RadarState.Stopped,
                    container.Length == 0 ? "Docker not running" : container,
                    error ?? "Docker Desktop is not running. Start it, then click Start.");
            var justStarted = container.Contains("second", StringComparison.OrdinalIgnoreCase);
            return Empty(justStarted ? RadarState.Starting : RadarState.Unhealthy, container, error);
        }

        if (string.IsNullOrEmpty(container) || !running)
            container = "local python";

        using (health)
        using (queue)
        using (gpu)
        {
            var h = health.RootElement;
            var q = queue.RootElement;

            var worker = q.GetProperty("worker");
            var phase = worker.GetProperty("phase").GetString() ?? "idle";
            var job = worker.TryGetProperty("job", out var j) && j.ValueKind == JsonValueKind.String
                ? j.GetString() : null;

            // The estimate is optional: an older container will not send it.
            double? remaining = null;
            var etaBasis = "";
            if (worker.TryGetProperty("eta", out var eta) && eta.ValueKind == JsonValueKind.Object)
            {
                if (eta.TryGetProperty("remaining_s", out var rem)
                    && rem.ValueKind == JsonValueKind.Number)
                    remaining = rem.GetDouble();
                if (eta.TryGetProperty("basis", out var basis)
                    && basis.ValueKind == JsonValueKind.String)
                    etaBasis = basis.GetString() ?? "";
            }

            // Whole-queue drain estimate, alongside the per-job one.
            double? queueRemaining = null;
            var queueJobs = 0;
            if (q.TryGetProperty("eta", out var qEta) && qEta.ValueKind == JsonValueKind.Object)
            {
                if (qEta.TryGetProperty("remaining_s", out var qr)
                    && qr.ValueKind == JsonValueKind.Number)
                    queueRemaining = qr.GetDouble();
                if (qEta.TryGetProperty("jobs", out var qj)
                    && qj.ValueKind == JsonValueKind.Number)
                    queueJobs = qj.GetInt32();
            }

            var waiting = q.GetProperty("waiting").GetInt32();
            var deferred = q.GetProperty("deferred_by_gpu").GetArrayLength();
            var gpuBusy = q.GetProperty("gpu").GetProperty("busy").GetBoolean();
            var gpuSummary = q.GetProperty("gpu").GetProperty("summary").GetString() ?? "";

            var llm = h.GetProperty("llm");
            var llmReady = llm.GetProperty("ready").GetBoolean();
            var llmDetail = llm.GetProperty("detail").GetString() ?? "";

            var upcoming = new List<(string, string)>();
            foreach (var u in h.GetProperty("upcoming").EnumerateArray())
            {
                var id = u.GetProperty("id").GetString() ?? "?";
                var next = u.TryGetProperty("next_run", out var n) && n.ValueKind == JsonValueKind.String
                    ? n.GetString()! : "-";
                upcoming.Add((id, next));
            }

            var gpus = new List<GpuInfo>();
            if (gpu is not null)
            {
                foreach (var g in gpu.RootElement.GetProperty("gpus").EnumerateArray())
                    gpus.Add(new GpuInfo(
                        g.GetProperty("index").GetInt32(),
                        g.GetProperty("name").GetString() ?? "?",
                        g.GetProperty("util_pct").GetInt32(),
                        g.GetProperty("vram_used_mib").GetInt32(),
                        g.GetProperty("vram_total_mib").GetInt32()));
            }

            var state = phase switch
            {
                "deferred_gpu_busy" => RadarState.DeferredGpu,
                "idle" => RadarState.Idle,
                _ => RadarState.Working
            };

            return new RadarStatus(state, container, phase, job, waiting, deferred,
                gpuBusy, gpuSummary,
                h.GetProperty("scheduler_mode").GetString() ?? "?",
                llmDetail, llmReady, upcoming, gpus, error, remaining, etaBasis,
                queueRemaining, queueJobs);
        }
    }

    public async Task<(bool Ok, DownloadsSnapshot Snap)> GetDownloadsAsync()
    {
        try
        {
            using var doc = await GetJsonAsync("/downloads");
            var root = doc.RootElement;
            var folder = root.TryGetProperty("folder", out var f) ? f.GetString() ?? "" : "";
            var hfAuth = root.TryGetProperty("hf_auth", out var a) && a.ValueKind == JsonValueKind.True;
            var rows = new List<DownloadRow>();
            if (root.TryGetProperty("downloads", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var d in arr.EnumerateArray())
                    rows.Add(ParseDownload(d));
            }
            return (true, new DownloadsSnapshot(rows, folder, hfAuth));
        }
        catch (Exception)
        {
            return (false, new DownloadsSnapshot(Array.Empty<DownloadRow>(), "", false));
        }
    }

    private static DownloadRow ParseDownload(JsonElement d)
    {
        string? S(string name) => d.TryGetProperty(name, out var p) && p.ValueKind == JsonValueKind.String
            ? p.GetString() : null;
        long? N(string name)
        {
            if (!d.TryGetProperty(name, out var p)) return null;
            if (p.ValueKind == JsonValueKind.Number && p.TryGetInt64(out var v)) return v;
            return null;
        }
        return new DownloadRow
        {
            Id = S("id") ?? "",
            Title = S("title") ?? "",
            Url = S("url") ?? "",
            ModelId = S("model_id"),
            LicenseId = S("license_id"),
            Status = S("status") ?? "",
            Reason = S("reason"),
            Path = S("path"),
            Bytes = N("bytes"),
            FileName = S("file_name"),
            UpdatedAt = S("updated_at"),
        };
    }

    private async Task<JsonDocument> GetJsonAsync(string path)
    {
        var resp = await _http.GetAsync(cfg.BaseUrl + path);
        resp.EnsureSuccessStatusCode();
        return JsonDocument.Parse(await resp.Content.ReadAsStringAsync());
    }

    private static RadarStatus Empty(RadarState state, string container, string? error) =>
        new(state, container, "-", null, 0, 0, false, "-", "-", "-", false,
            Array.Empty<(string, string)>(), Array.Empty<GpuInfo>(), error);
}
