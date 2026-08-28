param(
    [Parameter(Mandatory=$true)]
    [string]$Version,

    [Parameter(Mandatory=$true)]
    [string]$BaseUrl
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if ($Version -notmatch '^[A-Za-z0-9._-]+$') {
    throw "Version inválida: $Version"
}

$BaseUrl = $BaseUrl.TrimEnd('/')
$releaseDir = Join-Path $root "release"
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null

$zipName = "tester-spin-$Version.zip"
$zipPath = Join-Path $releaseDir $zipName
$manifestPath = Join-Path $releaseDir "update.json"

Remove-Item $zipPath -Force -ErrorAction SilentlyContinue

Write-Host "Empaquetando commit actual con git archive..."
git archive --format=zip --output="$zipPath" HEAD
if ($LASTEXITCODE -ne 0) {
    throw "git archive falló"
}

$sha = (Get-FileHash -Algorithm SHA256 -Path $zipPath).Hash.ToLowerInvariant()
$manifest = [ordered]@{
    schema = "tester-spin-update/v1"
    version = $Version
    url = "$BaseUrl/$zipName"
    sha256 = $sha
    entrypoint = "run.py"
}

$manifest | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $manifestPath

Write-Host ""
Write-Host "Paquete:   $zipPath"
Write-Host "Manifest:  $manifestPath"
Write-Host "SHA-256:   $sha"
Write-Host ""
Write-Host "Subí ambos archivos al mismo origen público (R2, Pages, GitHub Releases público, etc.)."
Write-Host "Después configurá manifest_url en %LOCALAPPDATA%\Tester-Spin\updater.json."
