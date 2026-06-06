# Copy Tesseract, Poppler, and Paddle models into vendor/ for offline builds.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Vendor = Join-Path $Root "vendor"
$Python = Join-Path $Root ".venv\Scripts\python.exe"

Write-Host "=== Populating vendor/ ===" -ForegroundColor Cyan

# Tesseract binary + bundled top-20 language packs
$TessSrc = "C:\Program Files\Tesseract-OCR"
$TessDst = Join-Path $Vendor "tesseract"
if (Test-Path $TessSrc) {
    if (Test-Path $TessDst) { Remove-Item $TessDst -Recurse -Force }
    Write-Host "Copying Tesseract..." -ForegroundColor Yellow
    Copy-Item $TessSrc $TessDst -Recurse
    Write-Host "  OK: $TessDst" -ForegroundColor Green
} else {
    Write-Warning "Tesseract not found at $TessSrc — install from UB-Mannheim wiki"
}

& (Join-Path $Root "scripts\bundle_tessdata.ps1")

# Poppler (winget layout)
$PopDst = Join-Path $Vendor "poppler\bin"
$PopFound = $null
$WingetRoot = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
if (Test-Path $WingetRoot) {
    $PopBin = Get-ChildItem -Path $WingetRoot -Recurse -Filter "pdftoppm.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($PopBin) {
        $PopFound = $PopBin.DirectoryName
    }
}
if (-not $PopFound) {
    $OnPath = Get-Command pdftoppm -ErrorAction SilentlyContinue
    if ($OnPath) { $PopFound = $OnPath.Source | Split-Path -Parent }
}
if ($PopFound) {
    if (Test-Path (Join-Path $Vendor "poppler")) { Remove-Item (Join-Path $Vendor "poppler") -Recurse -Force }
    New-Item -ItemType Directory -Path $PopDst -Force | Out-Null
    Write-Host "Copying Poppler from $PopFound ..." -ForegroundColor Yellow
    Copy-Item (Join-Path $PopFound "*") $PopDst -Recurse -Force
    Write-Host "  OK: $PopDst" -ForegroundColor Green
} else {
    Write-Warning "Poppler not found — install via winget install poppler"
}

# Paddle models
if (-not (Test-Path $Python)) {
    Write-Warning "No .venv — skip Paddle model download"
    exit 0
}

Write-Host "Downloading PaddleOCR models - may take several minutes..." -ForegroundColor Yellow
& $Python (Join-Path $Root "scripts\download_paddle_models.py")
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Paddle model download failed — run manually after pip install paddleocr"
    exit 1
}

Write-Host ""
Write-Host "Vendor folder ready." -ForegroundColor Green
