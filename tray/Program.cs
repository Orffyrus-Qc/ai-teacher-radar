using System.Diagnostics;

namespace RadarTray;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        // One tray icon is enough; a second instance just confuses the status.
        using var single = new Mutex(true, @"Global\AITeacherRadarTray", out var isFirst);
        if (!isFirst)
        {
            MessageBox.Show("App already running in the system tray.",
                "AI Teacher Radar", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }

        ApplicationConfiguration.Initialize();

        var cfg = AppConfig.Load();
        if (!File.Exists(cfg.ComposeFile))
        {
            var answer = MessageBox.Show(
                $"docker-compose.yml not found at:\n{cfg.ComposeFile}\n\n" +
                "Pick it now? (the choice is saved to radartray.json)",
                "AI Teacher Radar", MessageBoxButtons.YesNo, MessageBoxIcon.Warning);
            if (answer != DialogResult.Yes) return;

            using var dialog = new OpenFileDialog
            {
                Title = "Select docker-compose.yml",
                Filter = "Compose file (*.yml;*.yaml)|*.yml;*.yaml|All files (*.*)|*.*"
            };
            if (dialog.ShowDialog() != DialogResult.OK) return;

            cfg.ComposeFile = dialog.FileName;
            var root = Path.GetDirectoryName(dialog.FileName);
            if (root is not null) cfg.OutputFolder = Path.Combine(root, "out");
            cfg.Save();
        }

        if (!DockerPresent())
        {
            MessageBox.Show("docker was not found on PATH. Start Docker Desktop and try again.",
                "AI Teacher Radar", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        // The window opens on startup by default. --minimized suppresses it,
        // which is what you want from the Startup folder; --reports forces it
        // even if OpenWindowOnStartup has been turned off in the config.
        var minimized = args.Any(a => a.Equals("--minimized", StringComparison.OrdinalIgnoreCase)
                                   || a.Equals("/minimized", StringComparison.OrdinalIgnoreCase));
        var forced = args.Any(a => a.Equals("--reports", StringComparison.OrdinalIgnoreCase));
        var openWindow = forced || (cfg.OpenWindowOnStartup && !minimized);

        Application.Run(new TrayApp(cfg, openWindow));
    }

    private static bool DockerPresent()
    {
        try
        {
            using var p = Process.Start(new ProcessStartInfo("docker", "--version")
            {
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true
            });
            if (p is null) return false;
            p.WaitForExit(10_000);
            return p.HasExited && p.ExitCode == 0;
        }
        catch
        {
            return false;
        }
    }
}
