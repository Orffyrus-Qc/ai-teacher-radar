# RadarTray

A small Windows tray app to start, stop and watch the AI Teacher Radar.
.NET 9 WinForms, one self-contained `RadarTray.exe`, no installer.

## Build

```bash
dotnet publish -c Release -r win-x64 -o publish
```

Output: `tray/publish/RadarTray.exe`.

## The icon is the status

| Colour | State | Meaning |
|---|---|---|
| ⬤ Green | `Idle` | Alive, nothing queued — the normal resting state |
| ⬤ Blue | `Working` | A job is harvesting or writing a brief |
| ⬤ Amber | `DeferredGpu` | A run is waiting for your GPU to free up |
| ⬤ Red | `Unhealthy` | Container is up but the API is failing |
| ⬤ Purple | `Starting` | Container came up, API still booting |
| ⬤ Grey (crossed) | `Stopped` | Container not running |

Hover for a one-line summary. Balloon notifications fire only on **transitions**
that matter — a run deferred, a run finished, the service dropped — not on every
poll.

## Menu

- **Start / Stop / Restart** — wraps `docker compose` against the compose file
- **Run now →** Search · Daily synthesis · Weekly synthesis (posts to `/jobs`;
  the balloon repeats the API's own GPU note, so you learn immediately whether
  it started or was deferred)
- **Open app window** — the reports reader; double-clicking the tray icon does
  the same, so opening from the tray and from the desktop always give you the
  same interface
- **Open briefs folder** — the `out/` directory
- **Open API health** — `/health` in the browser
- **Radar status (diagnostics)…** — the separate troubleshooting dashboard
  (container status, per-GPU bars, next scheduled runs). Deliberately not what
  the tray icon opens: two different-looking windows for "open the app" reads
  as two different builds.

## Launching

The app window opens on startup, so a double-click always shows something
rather than silently placing a tray icon. Launching it a second time while it
is already running reports *"App already running in the system tray."* and
exits, leaving the original instance untouched.

| Flag | Effect |
|---|---|
| *(none)* | Opens the window and sits in the tray |
| `--minimized` | Tray only, no window — use this for a `shell:startup` shortcut, where a window on every login is noise |
| `--reports` | Forces the window open even if `OpenWindowOnStartup` is false |

`OpenWindowOnStartup` in `radartray.json` changes the default.

## Reports reader

**Reports…** in the tray menu opens a reader over everything the radar has
written; it is also what the startup window shows.

Left: every brief, newest first, grouped by day — with the weekly synthesis and
each day's synthesis above that day's searches. Columns are `When` (slot time,
`day`, or the ISO week), `Kind`, and item count, colour-coded by kind. The filter
box matches date, kind or title.

The bottom strip shows which file you are reading on the left and the live
radar state on the right — colour-coded on the same scale as the tray icon — so
this one window covers both reading and monitoring.

Right: the Markdown, rendered — headings, bold/italic, inline code, bullets,
tables laid out monospaced, and **clickable links** that open in your browser.
YAML front matter is hidden.

A `FileSystemWatcher` refreshes the list when a run finishes mid-read, and
clicking a "Run finished" balloon jumps straight here. `Open in editor` (or
double-clicking a row) opens the raw `.md` in your default editor.

The renderer is hand-rolled against the subset of Markdown the radar emits —
that keeps the app a single dependency-free exe rather than pulling in a
Markdown library and a browser control.

## Status window

Live at the poll interval: container status, worker phase and current job id,
queue depth (and how much of it is GPU-deferred), the GPU gate's own summary,
scheduler mode, LLM readiness, per-GPU utilisation/VRAM bars, and the next
scheduled runs with a countdown.

Closing it hides it — the tray keeps polling. Per-GPU telemetry is only fetched
while the window is open, so the resting cost is two small JSON calls every
5 seconds.

## Configuration

Optional `radartray.json` beside the exe. Defaults match this machine:

```json
{
  "ComposeFile": "F:\\CLAUDE\\ai-teacher-radar\\docker-compose.yml",
  "ContainerName": "ai-teacher-radar",
  "BaseUrl": "http://127.0.0.1:8791",
  "OutputFolder": "F:\\CLAUDE\\ai-teacher-radar\\out",
  "PollSeconds": 5,
  "NotifyOnStateChange": true
}
```

If the compose file is missing at startup the app offers a file picker and
saves your choice.

## Start with Windows

Drop a shortcut to `RadarTray.exe` in:

```
shell:startup
```

A mutex keeps a second instance from starting.
