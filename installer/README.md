# AITeacherRadar.msi

A per-machine Windows installer for the radar and its tray app. Built with
WiX 5, about **528 KB**.

## Build

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File installer/build.ps1
```

Publishes the tray app, pins the WiX UI extension to the CLI's own version, and
emits `installer/AITeacherRadar.msi`.

Needs the WiX CLI once: `dotnet tool install --global wix`.

## Install on the target machine

```bash
msiexec /i AITeacherRadar.msi
```

Silent, for a scripted rollout:

```bash
msiexec /i AITeacherRadar.msi /qn
```

Skip the desktop shortcut with `ADDLOCAL=Main`. Uninstall with `msiexec /x AITeacherRadar.msi /qn`,
or from Add/Remove Programs.

## What is in the package

The tray app, the radar's Python source, the Dockerfile, `config/*.yaml`, the
n8n workflow, `docs/TEACHER_MODELS.md` and the scripts — 32 files into
`%ProgramFiles%\AI Teacher Radar`, plus Start Menu and desktop shortcuts.

**The 277 MB container image is deliberately not included.** It is built on the
target from the shipped Dockerfile the first time you press Start, which needs
the same internet connection you already need for the Ollama models. Shipping a
pre-built image tarball would still not make the install offline-capable,
because the teacher/student models are tens of gigabytes.

## What is NOT in the package — install these first

| Requirement | Why | Where |
|---|---|---|
| **Docker Desktop** | The radar runs as a container | <https://docs.docker.com/desktop/install/windows-install/> |
| **.NET 9 Desktop Runtime** | The tray app | <https://dotnet.microsoft.com/download/dotnet/9.0> |
| **Ollama + a model or two** | Writes the summaries | <https://ollama.com> |
| NVIDIA GPU + container toolkit | *Optional* — real VRAM telemetry for the GPU gate | Docker Desktop GPU support |
| n8n | *Optional* — owns the schedule; otherwise the radar self-schedules | already yours, or skip |

Run **Start Menu → AI Teacher Radar → Check prerequisites** after installing. It
reports exactly what is missing and changes nothing; `Setup.ps1 -PullModels`
will pull the two default models for you.

## Where things live after installing

| Path | Contents |
|---|---|
| `%ProgramFiles%\AI Teacher Radar` | Read-only program files |
| `%LOCALAPPDATA%\AITeacherRadar\out` | The briefs and syntheses |
| `%LOCALAPPDATA%\AITeacherRadar\data` | SQLite item store and job queue |
| `%LOCALAPPDATA%\AITeacherRadar\.env` | Generated on first Start |
| `%LOCALAPPDATA%\AITeacherRadar\radartray.json` | Tray settings, if you change any |

Program Files is read-only at runtime, so everything writable goes to
`%LOCALAPPDATA%`. The tray app generates the `.env` on first Start with this
machine's IANA timezone and absolute paths, and passes it to compose with
`--env-file`; `docker-compose.yml` falls back to `./out` and `./data` so a
developer checkout keeps working unchanged.

## First run

1. Install the prerequisites above.
2. Launch **AI Teacher Radar** — it appears in the tray.
3. Right-click → **Start**. The first start runs `docker compose up -d --build`,
   so it takes a few minutes while the image builds. The icon goes purple
   (starting) then green (idle).
4. Right-click → **Run now → Search** to prove the whole path end to end.
5. Optionally import `n8n\ai-teacher-radar.workflow.json` into n8n and put both
   containers on the same docker network.

## Uninstall

Removes the program files, shortcuts and registry entries. It deliberately
leaves `%LOCALAPPDATA%\AITeacherRadar` alone so your briefs survive; delete that
folder by hand if you want a clean slate. The container and image are not
removed either — `docker compose -p ai-teacher-radar down --rmi local` does that.

## Verifying a build

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File installer/test-install.ps1
```

Installs (without the desktop shortcut, so it cannot clobber an existing one),
asserts every file and shortcut landed, checks the Add/Remove Programs entry and
that `docker compose` can parse the installed compose file, then uninstalls and
asserts the removal.
