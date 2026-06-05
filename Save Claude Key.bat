@echo off

cd /d "%~dp0"

title Save Claude Key

echo.

echo  Paste your Claude API key (from console.anthropic.com)

echo.

start "" "https://console.anthropic.com/settings/keys"

set /p CLAUDE_KEY="Paste key, press Enter: "

if "%CLAUDE_KEY%"=="" ( echo Nothing pasted. & pause & exit /b 1 )

python scripts\save_claude_key.py "%CLAUDE_KEY%"

echo.

pause

