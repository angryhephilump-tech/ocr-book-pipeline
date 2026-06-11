@echo off

cd /d "%~dp0"

title Finish PDF Transcribe



python -m pip install -q -r requirements-pdf-transcribe.txt 2>nul



python -c "from pdf_transcribe import load_settings; import sys; sys.exit(0 if load_settings().get('api_key') else 1)"

if errorlevel 1 (

    echo.

    echo   No Claude API key saved. Run "Launch PDF Transcribe.bat" first to save your key.

    pause

    exit /b 1

)



echo.

echo  Finishing Historia Verdadera job (reconcile / spot-check / transcribed.txt)...

echo  Run 1 and Run 2 will NOT be re-run.

echo.

python scripts\finish_job.py "C:\Users\drewc\Projects\ocr-book-pipeline\_pdf_transcribe_uploads\historia_verdadera_tomo_1_genaro_garc_a_s_edition\historiaverdade04castgoog_output"

if errorlevel 1 pause
