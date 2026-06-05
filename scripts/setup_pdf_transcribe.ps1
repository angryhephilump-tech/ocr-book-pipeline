# One-time setup for PDF Transcribe (dependencies + optional Poppler hint)
$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

Write-Host ""
Write-Host "  PDF Transcribe setup" -ForegroundColor Cyan
Write-Host ""

Write-Host "Installing Python packages..."
python -m pip install -r requirements-pdf-transcribe.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$poppler = Get-Command pdftoppm -ErrorAction SilentlyContinue
if (-not $poppler) {
    $bundled = Join-Path $Root "vendor\poppler\bin\pdftoppm.exe"
    if (Test-Path $bundled) {
        Write-Host "Poppler: found in vendor folder." -ForegroundColor Green
    } else {
        Write-Host "Poppler: not found yet (needed when you run a PDF)." -ForegroundColor Yellow
        Write-Host "  Install with: winget install poppler" -ForegroundColor Yellow
        Write-Host "  Or see vendor/README.md" -ForegroundColor Yellow
    }
} else {
    Write-Host "Poppler: OK" -ForegroundColor Green
}

Write-Host ""
Write-Host "  Done. Double-click Launch PDF Transcribe.bat to start." -ForegroundColor Green
Write-Host ""
