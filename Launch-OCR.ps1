# Verbatim Studio — launch the web app
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Port = 5050

$env:PATH = "C:\Program Files\Tesseract-OCR;" +
  "C:\Users\drewc\AppData\Local\Microsoft\WinGet\Packages\oschwartz10612.Poppler_Microsoft.Winget.Source_8wekyb3d8bbwe\poppler-25.07.0\Library\bin;" +
  $env:PATH
$env:TESSDATA_PREFIX = "$env:APPDATA\tesseract\"

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
  Write-Host "Could not start Verbatim Studio." -ForegroundColor Red
  if (-not $server.HasExited) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
  Read-Host "Press Enter to close"
  exit 1
}

Start-Process "http://127.0.0.1:$Port/"

Write-Host ""
Write-Host "  Verbatim Studio is running" -ForegroundColor Green
Write-Host "  http://127.0.0.1:$Port/" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Drop your book pages in the browser." -ForegroundColor Yellow
Write-Host "  Press Enter here when you're done to stop the server." -ForegroundColor DarkGray
Write-Host ""
Read-Host "Press Enter to stop"

Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
