@echo off

cd /d "%~dp0"

echo.

echo  Installing PDF Transcribe packages (one time)...

echo.

python -m pip install -r requirements-pdf-transcribe.txt

if errorlevel 1 (

    echo.

    echo  FAILED - is Python installed? Try: winget install Python.Python.3.12

    pause

    exit /b 1

)

echo.

echo  All good. Next: double-click "Save DeepSeek Key.bat" then "Launch PDF Transcribe.bat"

echo.

pause

