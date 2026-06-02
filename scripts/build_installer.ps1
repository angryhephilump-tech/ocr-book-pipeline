# Build Archive Studios installer: vendor -> PyInstaller -> copy bundles -> Inno Setup
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$DistName = "Archive Studios"
$DistDir = Join-Path $Root "dist\$DistName"

Set-Location $Root

Write-Host "=== Step 1: Populate vendor ===" -ForegroundColor Cyan
& (Join-Path $Root "scripts\populate_vendor.ps1")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "=== Step 2: Install build deps ===" -ForegroundColor Cyan
& $Python -m pip install -q -r requirements.txt -r build\requirements-build.txt

Write-Host "=== Step 3: PyInstaller ===" -ForegroundColor Cyan
if (Test-Path $DistDir) { Remove-Item $DistDir -Recurse -Force }
& $Python -m PyInstaller build\archive_studios.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "=== Step 4: Copy vendor bundles beside exe ===" -ForegroundColor Cyan
foreach ($name in @("tesseract", "poppler", "paddlex", "models")) {
    $src = Join-Path $Root "vendor\$name"
    $dst = Join-Path $DistDir $name
    if (Test-Path $src) {
        if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
        Copy-Item $src $dst -Recurse
        Write-Host "  Copied $name" -ForegroundColor Green
    } else {
        Write-Warning "  Missing vendor/$name"
    }
}

Write-Host "=== Step 5: Inno Setup ===" -ForegroundColor Cyan
$Iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($Iscc) {
    & $Iscc (Join-Path $Root "build\installer.iss")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "Installer: dist\ArchiveStudios-Setup.exe" -ForegroundColor Green
} else {
    Write-Warning "Inno Setup not found. Portable build at: $DistDir"
}

Write-Host ""
Write-Host "Build complete." -ForegroundColor Green
