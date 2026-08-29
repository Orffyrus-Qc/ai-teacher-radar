<#
.SYNOPSIS
  Builds AITeacherRadar.msi: publishes the tray app, then packages it with the
  radar's source via WiX 5.

.NOTES
  Requires the WiX CLI:  dotnet tool install --global wix
  The UI extension is added automatically if missing.
#>
[CmdletBinding()]
param(
    [string]$Version = '1.0.0.0',
    [string]$Output = 'AITeacherRadar.msi'
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$root = Split-Path -Parent $here

Write-Host "1/4  Publishing the tray app" -ForegroundColor Cyan
# The single-file bundler cannot overwrite a running exe.
Get-Process RadarTray -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1
dotnet publish (Join-Path $root 'tray\RadarTray.csproj') -c Release -r win-x64 --nologo -v q `
    -o (Join-Path $root 'tray\publish')
if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed" }

$exe = Join-Path $root 'tray\publish\RadarTray.exe'
if (-not (Test-Path $exe)) { throw "RadarTray.exe was not produced at $exe" }
Write-Host ("     " + [math]::Round((Get-Item $exe).Length / 1KB) + " KB") -ForegroundColor DarkGray

Write-Host "2/4  Checking WiX" -ForegroundColor Cyan
if (-not (Get-Command wix -ErrorAction SilentlyContinue)) {
    throw "wix not found. Install it with: dotnet tool install --global wix"
}
Write-Host ("     wix " + (wix --version)) -ForegroundColor DarkGray

Write-Host "3/4  Ensuring the UI extension" -ForegroundColor Cyan
# Pin to the CLI's own version: an unpinned add resolves to the WiX 6 build,
# which fails with "Could not find expected package root folder wixext5".
$wixVersion = ((wix --version) -split '\+')[0]
$ext = wix extension list -g 2>&1 | Out-String
if ($ext -notmatch 'WixToolset\.UI\.wixext') {
    wix extension add -g "WixToolset.UI.wixext/$wixVersion"
    if ($LASTEXITCODE -ne 0) { throw "could not add WixToolset.UI.wixext/$wixVersion" }
}
Write-Host "     WixToolset.UI.wixext $wixVersion ready" -ForegroundColor DarkGray

Write-Host "4/4  Building the MSI" -ForegroundColor Cyan
Push-Location $here
try {
    wix build Package.wxs -ext WixToolset.UI.wixext -arch x64 `
        -d Version=$Version -o $Output
    if ($LASTEXITCODE -ne 0) { throw "wix build failed" }
}
finally {
    Pop-Location
}

$msi = Join-Path $here $Output
Write-Host ""
Write-Host ("Built " + $msi) -ForegroundColor Green
Write-Host ("      " + [math]::Round((Get-Item $msi).Length / 1KB) + " KB") -ForegroundColor Gray
Write-Host ""
Write-Host "Install on the target machine with:" -ForegroundColor Gray
Write-Host "  msiexec /i `"$Output`"" -ForegroundColor White
Write-Host "or silently:" -ForegroundColor Gray
Write-Host "  msiexec /i `"$Output`" /qn" -ForegroundColor White
