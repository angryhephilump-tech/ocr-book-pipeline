# Archive Studios - launch the web app
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Port = 5050

if (-not (Test-Path $Python)) {
  Write-Host "Missing .venv - run: python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt" -ForegroundColor Red
  Read-Host "Press Enter to close"
  exit 1
}

Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }

$server = Start-Process -FilePath $Python `
  -ArgumentList "review_ui.py", "--port", $Port `
  -WorkingDirectory $Root `
  -PassThru -WindowStyle Hidden

$ready = $false
for ($i = 0; $i -lt 40; $i++) {
  Start-Sleep -Milliseconds 400
  if ($server.HasExited) { break }
  $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  if ($conn) { $ready = $true; break }
}

if (-not $ready) {
  Write-Host "Could not start Archive Studios." -ForegroundColor Red
  if (-not $server.HasExited) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
  Read-Host "Press Enter to close"
  exit 1
}

Start-Process "http://127.0.0.1:$Port/"

Write-Host ""
Write-Host "  Archive Studios is running" -ForegroundColor Green
Write-Host "  http://127.0.0.1:$Port/" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Drop your book pages in the browser." -ForegroundColor Yellow
Write-Host "  Press Enter here when you are done to stop the server." -ForegroundColor DarkGray
Write-Host ""
Read-Host "Press Enter to stop"

Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
