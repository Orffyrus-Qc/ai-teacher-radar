<#
.SYNOPSIS
  Thin wrapper over the AI Teacher Radar API.
.EXAMPLE
  .\scripts\radar.ps1 search
  .\scripts\radar.ps1 gpu
  .\scripts\radar.ps1 latest daily
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('health', 'gpu', 'queue', 'runs', 'jobs', 'search', 'daily', 'weekly', 'latest', 'logs', 'up', 'down', 'rebuild')]
    [string]$Command = 'health',

    [Parameter(Position = 1)]
    [string]$Arg
)

$ErrorActionPreference = 'Stop'
$Base = 'http://127.0.0.1:8791'
$Root = Split-Path -Parent $PSScriptRoot

function Show($obj) { $obj | ConvertTo-Json -Depth 6 }

function Enqueue([string]$kind) {
    $body = @{ kind = $kind } | ConvertTo-Json -Compress
    $r = Invoke-RestMethod -Uri "$Base/jobs" -Method Post -ContentType 'application/json' -Body $body
    Write-Host "queued $($r.job.id)" -ForegroundColor Green
    Write-Host $r.note -ForegroundColor $(if ($r.gpu_busy_at_submit) { 'Yellow' } else { 'Gray' })
    Write-Host "GPU: $($r.gpu)"
}

switch ($Command) {
    'health' { Show (Invoke-RestMethod "$Base/health") }
    'gpu' { Show (Invoke-RestMethod "$Base/gpu") }
    'queue' { Show (Invoke-RestMethod "$Base/queue") }
    'runs' { Show (Invoke-RestMethod "$Base/runs") }
    'jobs' { Show (Invoke-RestMethod "$Base/jobs") }
    'search' { Enqueue 'search' }
    'daily' { Enqueue 'daily' }
    'weekly' { Enqueue 'weekly' }
    'latest' {
        $kind = if ($Arg) { $Arg } else { 'search' }
        Invoke-RestMethod "$Base/latest?kind=$kind"
    }
    'logs' { docker compose -f "$Root\docker-compose.yml" logs -f --tail 100 ai-teacher-radar }
    'up' { docker compose -f "$Root\docker-compose.yml" up -d }
    'down' { docker compose -f "$Root\docker-compose.yml" down }
    'rebuild' { docker compose -f "$Root\docker-compose.yml" up -d --build }
}
