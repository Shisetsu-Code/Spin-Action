param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venv = Join-Path $root ".launcher-build"
$python = Join-Path $venv "Scripts\python.exe"

if ($Clean -and (Test-Path $venv)) {
    Remove-Item $venv -Recurse -Force
}

if (-not (Test-Path $python)) {
    Write-Host "Creando entorno de build del launcher..."
    py -3 -m venv $venv
}

Write-Host "Instalando/actualizando PyInstaller..."
& $python -m pip install --disable-pip-version-check -q --upgrade pip pyinstaller

$dist = Join-Path $root "dist"
$build = Join-Path $root "build\launcher"
$spec = Join-Path $root "Tester-Spin.spec"

if ($Clean) {
    Remove-Item $dist -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $build -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $spec -Force -ErrorAction SilentlyContinue
}

Write-Host "Compilando Tester-Spin.exe..."
& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "Tester-Spin" `
    --distpath $dist `
    --workpath $build `
    launcher.py

$exe = Join-Path $dist "Tester-Spin.exe"
if (-not (Test-Path $exe)) {
    throw "PyInstaller no generó $exe"
}

Write-Host ""
Write-Host "==============================================="
Write-Host "Launcher listo: $exe"
Write-Host "Ese EXE es el acceso estable autoactualizable."
Write-Host "==============================================="
