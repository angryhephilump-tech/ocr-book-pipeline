@echo off

set "BAT=%~dp0Launch PDF Transcribe.bat"

set "WORKDIR=%~dp0"

set "NAME=Deep Seek Transcribe.lnk"



powershell -NoProfile -ExecutionPolicy Bypass -Command ^

  "$d = [Environment]::GetFolderPath('Desktop');" ^

  "$w = New-Object -ComObject WScript.Shell;" ^

  "$s = $w.CreateShortcut((Join-Path $d '%NAME%'));" ^

  "$s.TargetPath = '%BAT%';" ^

  "$s.WorkingDirectory = '%WORKDIR%';" ^

  "$s.Description = 'DeepSeek PDF transcription';" ^

  "$s.Save();" ^

  "Write-Host ''; Write-Host ' Desktop shortcut: Deep Seek Transcribe (not the .bat file)'; Write-Host ''"



pause

