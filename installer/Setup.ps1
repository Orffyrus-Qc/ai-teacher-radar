<#
.SYNOPSIS
  Checks everything AI Teacher Radar needs on this machine and says plainly
  what is missing. Read-only by default: it changes nothing unless you pass
  -PullModels.

.EXAMPLE
  .\Setup.ps1
  .\Setup.ps1 -PullModels
#>
[CmdletBinding()]
param(
    [switch]$PullModels
)

$ErrorActionPreference = 'Continue'
$script:Missing = @()

function Head($text) {
    Write-Host ""
    Write-Host $text -ForegroundColor Cyan
    Write-Host ('-' * $text.Length) -ForegroundColor DarkGray
}

function Ok($text) { Write-Host "  [ OK ]   $text" -ForegroundColor Green }
function Warn($text) { Write-Host "  [ ??  ]  $text" -ForegroundColor Yellow }
function Bad($text) {
    Write-Host "  [FAIL]   $text" -ForegroundColor Red
    $script:Missing += $text
}

function Has([string]$exe) {
    return [bool](Get-Command $exe -ErrorAction SilentlyContinue)
}

Write-Host ""
Write-Host "AI Teacher Radar - prerequisite check" -ForegroundColor White
Write-Host "=====================================" -ForegroundColor DarkGray

# ---------------------------------------------------------------- Docker
Head "Docker"
if (-not (Has 'docker')) {
    Bad "docker is not on PATH. Install Docker Desktop: https://docs.docker.com/desktop/install/windows-install/"
}
else {
    Ok ("docker " + ((docker --version) -replace 'Docker version ', ''))
    docker info 2>&1 | Out-Null
    if ($?) { Ok "Docker engine is running" }
    else { Bad "Docker is installed but the engine is not running. Start Docker Desktop." }

    docker compose version 2>&1 | Out-Null
    if ($?) { Ok ("compose " + ((docker compose version) -replace 'Docker Compose version ', '')) }
    else { Bad "docker compose v2 is unavailable" }
}

# ------------------------------------------------------------ .NET runtime
Head ".NET desktop runtime (tray app)"
if (-not (Has 'dotnet')) {
    Warn "dotnet not on PATH. If the tray app starts, you can ignore this."
}
else {
    $desktop = dotnet --list-runtimes 2>$null | Where-Object { $_ -match 'Microsoft\.WindowsDesktop\.App 9\.' }
    if ($desktop) { Ok "Microsoft.WindowsDesktop.App 9.x present" }
    else { Bad "The .NET 9 Desktop Runtime is missing: https://dotnet.microsoft.com/download/dotnet/9.0" }
}

# ------------------------------------------------------------------- GPU
Head "GPU (optional - enables GPU-aware scheduling)"
if (-not (Has 'nvidia-smi')) {
    Warn "No nvidia-smi. The radar still runs; the GPU gate falls back to the ComfyUI and Ollama probes."
}
else {
    nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader | ForEach-Object { Ok $_ }
    Write-Host "  ...testing GPU access from inside a container (may pull a small image)" -ForegroundColor DarkGray
    $gpuTest = docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi -L 2>&1
    if ($LASTEXITCODE -eq 0) { Ok "Containers can see the GPU (real VRAM telemetry available)" }
    else { Warn "Containers cannot see the GPU. Comment out the 'deploy:' block in docker-compose.yml." }
}

# ---------------------------------------------------------------- Ollama
Head "Ollama (writes the summaries)"
$ollamaUp = $false
try {
    $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5
    $ollamaUp = $true
    Ok "Ollama is running with $($tags.models.Count) model(s)"
}
catch {
    Bad "Ollama is not answering on 127.0.0.1:11434. Install from https://ollama.com and start it."
}

# These two are the defaults in .env.example.
$wanted = @('qwen3.5:9b', 'gpt-oss:20b')
if ($ollamaUp) {
    $have = $tags.models | ForEach-Object { $_.name }
    foreach ($m in $wanted) {
        if ($have -contains $m) {
            Ok "model $m"
        }
        elseif ($PullModels) {
            Write-Host "  ...pulling $m (this is several GB)" -ForegroundColor DarkGray
            ollama pull $m
        }
        else {
            Warn "model $m is not pulled - run: ollama pull $m   (or re-run this script with -PullModels)"
        }
    }
    Write-Host "  Without these the radar still produces rule-based briefs, just no written summary." -ForegroundColor DarkGray
}

# ----------------------------------------------------------- orchestrator
Head "n8n (optional scheduler)"
if (Has 'docker') {
    # Match on the image name, not --filter ancestor=: that filter needs an
    # exact image reference including the tag, so it misses n8n:2.35.7.
    $n8n = docker ps --format "{{.Names}}`t{{.Image}}" 2>$null |
        Where-Object { $_ -match 'n8n' } |
        ForEach-Object { ($_ -split "`t")[0] }
    if ($n8n) {
        Ok "n8n container found: $($n8n -join ', ')"
        Write-Host "  Import n8n\ai-teacher-radar.workflow.json to let it own the schedule." -ForegroundColor DarkGray
        Write-Host "  Both containers must share a docker network for it to reach the radar." -ForegroundColor DarkGray
    }
    else {
        Warn "No n8n found. The radar self-schedules (SCHEDULER_MODE=auto), so this is fine."
    }
}

# -------------------------------------------------------------- writable
Head "Per-user data"
$root = Join-Path $env:LOCALAPPDATA 'AITeacherRadar'
foreach ($d in @($root, (Join-Path $root 'out'), (Join-Path $root 'data'))) {
    if (Test-Path $d) { Ok $d } else { Warn "$d (created on first Start)" }
}

# --------------------------------------------------------------- verdict
Write-Host ""
if ($script:Missing.Count -eq 0) {
    Write-Host "All required prerequisites are present." -ForegroundColor Green
    Write-Host "Launch 'AI Teacher Radar' and press Start - the first start builds the" -ForegroundColor Gray
    Write-Host "container image, which takes a couple of minutes and needs internet." -ForegroundColor Gray
}
else {
    Write-Host "$($script:Missing.Count) blocking problem(s):" -ForegroundColor Red
    $script:Missing | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
}
Write-Host ""
