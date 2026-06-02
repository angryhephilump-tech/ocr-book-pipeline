# Simulate clean-machine launch: no .venv on PATH, run bundled exe only.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$DistDir = Join-Path $Root "dist\Archive Studios"
$Exe = Join-Path $DistDir "Archive Studios.exe"
$Port = 5051
$LogFile = Join-Path $Root "docs\clean_machine_test_result.txt"

if (-not (Test-Path $Exe)) {
    "FAIL: Exe not found at $Exe" | Out-File $LogFile -Encoding utf8
    exit 1
}

$checks = @()
function Add-Check($name, $ok, $detail) {
    $script:checks += [pscustomobject]@{ Name = $name; Ok = $ok; Detail = $detail }
}

Add-Check "exe_exists" $true $Exe
Add-Check "tesseract_bundled" (Test-Path (Join-Path $DistDir "tesseract\tesseract.exe")) (Join-Path $DistDir "tesseract")
Add-Check "poppler_bundled" (Test-Path (Join-Path $DistDir "poppler\bin\pdftoppm.exe")) (Join-Path $DistDir "poppler")
Add-Check "paddlex_bundled" (Test-Path (Join-Path $DistDir "paddlex\official_models")) (Join-Path $DistDir "paddlex")

$cleanPath = @(
    "$env:SystemRoot\system32",
    "$env:SystemRoot",
    $DistDir,
    (Join-Path $DistDir "poppler\bin"),
    (Join-Path $DistDir "tesseract")
) -join ";"

$env:PATH = $cleanPath
$env:VERBATIM_DEV = "1"
$env:FLAGS_use_mkldnn = "0"
$env:PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK = "True"
Remove-Item Env:\VIRTUAL_ENV -ErrorAction SilentlyContinue

Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }

$proc = Start-Process -FilePath $Exe -ArgumentList "--port", $Port -WorkingDirectory $DistDir -PassThru -WindowStyle Hidden
$ready = $false
for ($i = 0; $i -lt 50; $i++) {
    Start-Sleep -Milliseconds 500
    if ($proc.HasExited) { break }
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conn) { $ready = $true; break }
}

if ($proc.HasExited) {
    Add-Check "server_start" $false "Process exited early (code $($proc.ExitCode))"
} elseif (-not $ready) {
    Add-Check "server_start" $false "Port $Port not listening"
} else {
    Add-Check "server_start" $true "http://127.0.0.1:$Port/"
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 15
        $okHome = $resp.StatusCode -eq 200 -and $resp.Content -match "Archive"
        Add-Check "http_home" $okHome "Status $($resp.StatusCode)"
    } catch {
        Add-Check "http_home" $false $_.Exception.Message
    }
}

Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue

$passed = ($checks | Where-Object { -not $_.Ok }).Count -eq 0
$lines = @("Archive Studios clean-machine test", "Time: $(Get-Date -Format o)", "")
foreach ($c in $checks) {
    $status = if ($c.Ok) { "PASS" } else { "FAIL" }
    $lines += "$status  $($c.Name)  $($c.Detail)"
}
$lines += ""
$lines += "Overall: $(if ($passed) { 'PASS' } else { 'FAIL' })"
$lines | Out-File $LogFile -Encoding utf8
Write-Host ($lines -join "`n")
if (-not $passed) { exit 1 }
