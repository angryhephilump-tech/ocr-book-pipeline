@echo off

cd /d "%~dp0"

echo.

echo  === Save your DeepSeek API key ===

echo.

echo  Get one at: https://platform.deepseek.com  (API Keys - Create)

echo.

set /p DEEPSEEK_KEY="Paste your key here, then press Enter: "

if "%DEEPSEEK_KEY%"=="" (

    echo.

    echo  Nothing pasted. Try again.

    pause

    exit /b 1

)

python scripts\save_deepseek_key.py "%DEEPSEEK_KEY%"

echo.

pause

