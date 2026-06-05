$Root = Split-Path $PSScriptRoot -Parent
$Launcher = Join-Path $Root "Launch PDF Transcribe.bat"
$Desktop = [Environment]::GetFolderPath("Desktop")
$Lnk = Join-Path $Desktop "Deep Seek Transcribe.lnk"
$OldBat = Join-Path $Desktop "Deep Seek Transcribe.bat"

if (Test-Path $OldBat) {
    Remove-Item $OldBat -Force
    Write-Host "Removed old duplicate: Deep Seek Transcribe.bat" -ForegroundColor Yellow
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($Lnk)
$shortcut.TargetPath = $Launcher
$shortcut.WorkingDirectory = $Root
$shortcut.Description = "PDF transcription (3 passes + diff)"
$shortcut.WindowStyle = 1
$shortcut.Save()

Write-Host ""
Write-Host "Desktop shortcut (use this one):" -ForegroundColor Green
Write-Host "  $Lnk"
Write-Host ""
Write-Host "Opening Desktop folder..." -ForegroundColor Cyan
Start-Process explorer.exe $Desktop
