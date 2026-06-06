$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
  Write-Host "Missing .venv. Create it and install requirements first." -ForegroundColor Red
  exit 1
}

if (-not $env:ARCHIVE_GUMROAD_PRODUCT_ID) {
  Write-Warning "ARCHIVE_GUMROAD_PRODUCT_ID is not set."
}
if (-not $env:ARCHIVE_HF_TOKEN) {
  Write-Warning "ARCHIVE_HF_TOKEN is not set."
}

& $Python -m uvicorn gateway.app:app --host 127.0.0.1 --port 8787
