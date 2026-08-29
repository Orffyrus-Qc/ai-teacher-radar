<#
.SYNOPSIS
  Install / verify / uninstall round-trip for AITeacherRadar.msi.

  ADDLOCAL=Main deliberately omits the DesktopShortcut feature so a test run on
  a development machine cannot overwrite a hand-made desktop shortcut of the
  same name.
#>
param([switch]$KeepInstalled)

$ErrorActionPreference = 'Stop'
$msi = Join-Path $PSScriptRoot 'AITeacherRadar.msi'
$log = Join-Path $env:TEMP 'radar-msi.log'
$installDir = Join-Path ${env:ProgramFiles} 'AI Teacher Radar'
$menu = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\AI Teacher Radar'
$fail = 0

function Check($label, $condition) {
    if ($condition) { Write-Host "  OK    $label" -ForegroundColor Green }
    else { Write-Host "  FAIL  $label" -ForegroundColor Red; $script:fail++ }
}

Write-Host "Installing..." -ForegroundColor Cyan
$p = Start-Process msiexec -ArgumentList @('/i', "`"$msi`"", '/qn', 'ADDLOCAL=Main', '/l*v', "`"$log`"") -Wait -PassThru
Write-Host "  msiexec exit code: $($p.ExitCode)"
if ($p.ExitCode -ne 0) {
    Write-Host "Install failed; last lines of the log:" -ForegroundColor Red
    Get-Content $log -Tail 25
    exit 1
}

Write-Host "`nVerifying..." -ForegroundColor Cyan
Check "install folder exists"            (Test-Path $installDir)
Check "RadarTray.exe"                    (Test-Path (Join-Path $installDir 'RadarTray.exe'))
Check "docker-compose.yml"               (Test-Path (Join-Path $installDir 'docker-compose.yml'))
Check "Dockerfile"                       (Test-Path (Join-Path $installDir 'Dockerfile'))
Check "app\main.py"                      (Test-Path (Join-Path $installDir 'app\main.py'))
Check "app\sources\huggingface.py"       (Test-Path (Join-Path $installDir 'app\sources\huggingface.py'))
Check "config\sources.yaml"              (Test-Path (Join-Path $installDir 'config\sources.yaml'))
Check "docs\TEACHER_MODELS.md"           (Test-Path (Join-Path $installDir 'docs\TEACHER_MODELS.md'))
Check "n8n workflow"                     (Test-Path (Join-Path $installDir 'n8n\ai-teacher-radar.workflow.json'))
Check "scripts\Setup.ps1"                (Test-Path (Join-Path $installDir 'scripts\Setup.ps1'))
Check "Start Menu folder"                (Test-Path $menu)
Check "Start Menu: app shortcut"         (Test-Path (Join-Path $menu 'AI Teacher Radar.lnk'))
Check "Start Menu: reports shortcut"     (Test-Path (Join-Path $menu 'AI Teacher Radar Reports.lnk'))
Check "Start Menu: prerequisites"        (Test-Path (Join-Path $menu 'Check prerequisites.lnk'))

$arp = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -eq 'AI Teacher Radar' }
Check "listed in Add/Remove Programs"    ($null -ne $arp)
if ($arp) { Write-Host "        version $($arp.DisplayVersion), $([math]::Round($arp.EstimatedSize/1KB,2)) MB" -ForegroundColor DarkGray }

# The compose file must be usable straight from Program Files.
Push-Location $installDir
try {
    docker compose -f (Join-Path $installDir 'docker-compose.yml') -p radar-msi-test config 2>&1 | Out-Null
    Check "docker compose parses the installed compose file" ($LASTEXITCODE -eq 0)
}
finally { Pop-Location }

if ($KeepInstalled) {
    Write-Host "`nLeaving it installed (-KeepInstalled)." -ForegroundColor Yellow
}
else {
    Write-Host "`nUninstalling..." -ForegroundColor Cyan
    $u = Start-Process msiexec -ArgumentList @('/x', "`"$msi`"", '/qn') -Wait -PassThru
    Write-Host "  msiexec exit code: $($u.ExitCode)"
    Check "install folder removed"  (-not (Test-Path $installDir))
    Check "Start Menu folder removed" (-not (Test-Path $menu))
}

Write-Host ""
if ($fail -eq 0) { Write-Host "All checks passed." -ForegroundColor Green }
else { Write-Host "$fail check(s) failed." -ForegroundColor Red }
