@echo off

cd /d "%~dp0"

title PDF Transcribe



python -m pip install -q -r requirements-pdf-transcribe.txt 2>nul



powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }" 2>nul

timeout /t 1 /nobreak >nul



python -c "from pdf_transcribe import load_settings; import sys; sys.exit(0 if load_settings().get('api_key') else 1)"

if errorlevel 1 goto NEEDKEY

goto RUN



:NEEDKEY

echo.

echo  ============================================================

echo   FIRST TIME - paste your Claude API key

echo  ============================================================

echo.

echo   Get a key here (opens in browser):

echo   https://console.anthropic.com/settings/keys

echo.

start "" "https://console.anthropic.com/settings/keys"

echo.

set /p CLAUDE_KEY="Paste key here, then press Enter: "

if "%CLAUDE_KEY%"=="" (

    echo.

    echo   You did not paste anything. Run this file again when ready.

    pause

    exit /b 1

)

python scripts\save_claude_key.py "%CLAUDE_KEY%"

if errorlevel 1 (

    echo   Save failed.

    pause

    exit /b 1

)

echo.

echo   Key saved on your computer. Starting the app...

echo.

timeout /t 2 /nobreak >nul



:RUN

echo  Opening PDF Transcribe in your browser...

echo  (Leave this window open while it runs.)

echo.

python pdf_transcribe_ui.py

if errorlevel 1 pause

