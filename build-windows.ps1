# Build docker-migrate.exe (single-file) with PyInstaller on Windows.
# Requires: Python 3.10+ and Docker CLI on PATH (runtime, not build time).
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Find-Python {
    foreach ($cmd in @("python", "python3", "py")) {
        if (Get-Command $cmd -ErrorAction SilentlyContinue) {
            return $cmd
        }
    }
    throw "Python 3.10+ が見つかりません。https://www.python.org/downloads/ からインストールしてください。"
}

$Python = Find-Python
& $Python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.10 以上が必要です。"
}

Write-Host "Installing build dependencies..."
& $Python -m pip install --upgrade pip pyinstaller

Write-Host "Building docker-migrate.exe..."
& $Python -m PyInstaller --noconfirm --clean docker-migrate.spec

$Out = Join-Path $Root "dist\docker-migrate.exe"
if (-not (Test-Path $Out)) {
    throw "Build failed: $Out was not created."
}

Write-Host ""
Write-Host "Built: $Out"
Write-Host "Usage:"
Write-Host "  .\dist\docker-migrate.exe"
Write-Host "  .\dist\docker-migrate.exe export my-app -o .\docker-backup\my-app"
