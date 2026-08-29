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
    string? Error)
{
    public string Headline => State switch
    {
        RadarState.Stopped => "Stopped",
        RadarState.Starting => "Starting…",
        RadarState.Unhealthy => "Unhealthy",
        RadarState.Idle => "Alive — idle",
        RadarState.Working => $"Running — {WorkerPhase}",
        RadarState.DeferredGpu => "Deferred — GPU busy",
        _ => "Unknown"
    };
}

public sealed record GpuInfo(int Index, string Name, int UtilPct, int VramUsedMib, int VramTotalMib);

public sealed class RadarClient(AppConfig cfg)
{
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(6) };

    // ----------------------------------------------------------- docker
    // --build so a freshly installed machine, with no image yet, comes up on
    // the first click of Start instead of failing.
    public Task<(bool Ok, string Output)> StartAsync() =>
        ComposeAsync("up -d --build");

    public Task<(bool Ok, string Output)> StopAsync() =>
        ComposeAsync("down");

    public async Task<(bool Ok, string Output)> RestartAsync()
    {
        var (ok, output) = await ComposeAsync("restart");
        return (ok, output);
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
                UseShellExecute = false,
                CreateNoWindow = true,
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
    public async Task<(bool Ok, string Body)> PostJobAsync(string kind)
    {
        try
        {
            var content = new StringContent($"{{\"kind\":\"{kind}\"}}", Encoding.UTF8, "application/json");
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

        if (string.IsNullOrEmpty(container) || !running)
            return Empty(RadarState.Stopped, container.Length == 0 ? "not created" : container, null);

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
            // Up but not answering: almost always the 15s start-up window.
            var justStarted = container.Contains("second", StringComparison.OrdinalIgnoreCase);
            return Empty(justStarted ? RadarState.Starting : RadarState.Unhealthy, container, error);
        }

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
                llmDetail, llmReady, upcoming, gpus, error);
        }
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
