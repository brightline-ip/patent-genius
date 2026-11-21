# Shows whether this Patent Genius installation is ready: Python, the data folder, your scope, the Hub and Tesseract.
# Makes no network call and prints no key (there are none).
param([switch]$NoPause)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
$code = 0
try {
    $config = Read-PgConfig $root
    $paths = Get-PgPaths $root ([string]$config.data_root)
    if (-not (Test-Path -LiteralPath $paths.PgCmd -PathType Leaf)) { throw 'PatentGenius.cmd is missing. Run the "Patent Genius - Repair" shortcut.' }
    Write-Host 'Patent Genius - Check' -ForegroundColor White
    Write-Info 'No network call is made.'
    Write-Host ''
    $report = Invoke-PgDoctor $paths
    foreach ($line in $report.Lines) { Write-Info $line }
    if ($report.Code -ne 0) { $code = 1 }
    Write-Host ''
    Write-Info "Scope file: $($paths.Scope)"
    Write-Info "Data folder: $($paths.DataRoot)"
    Write-Info 'If the Hub shows as missing, install the Patent Research Hub 0.2.0 or later and run the Repair shortcut.'
} catch {
    Write-Bad $_.Exception.Message
    $code = 1
}
if (-not $NoPause) { Read-Host 'Press Enter to close' | Out-Null }
exit $code
