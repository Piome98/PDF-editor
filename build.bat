@echo off
rem Build dist\PdfPriceEditor.exe
rem Internet is needed only the first time (libraries + OCR models). The exe runs offline.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creating virtual environment and installing libraries...
    python -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
)

echo [2/4] Preparing OCR models...
".venv\Scripts\python.exe" tools\fetch_models.py || goto :error

echo [3/4] Running tests...
".venv\Scripts\python.exe" -m pytest -q || goto :error

echo [4/4] Building exe...
set RAPID=.venv\Lib\site-packages\rapidocr
".venv\Scripts\pyinstaller.exe" --noconfirm --clean --onefile --windowed ^
    --name PdfPriceEditor ^
    --add-data "models;models" ^
    --add-data "%RAPID%\config.yaml;rapidocr" ^
    --add-data "%RAPID%\default_models.yaml;rapidocr" ^
    --collect-submodules rapidocr ^
    --exclude-module tkinter --exclude-module pytest ^
    --exclude-module torch --exclude-module paddle --exclude-module openvino ^
    --exclude-module tensorrt --exclude-module MNN --exclude-module matplotlib ^
    --workpath "%TEMP%\pdfprice_build" --specpath "%CD%" ^
    --distpath dist ^
    main.py || goto :error

echo.
echo Done: %~dp0dist\PdfPriceEditor.exe
pause
exit /b 0

:error
echo.
echo BUILD FAILED - see the messages above.
pause
exit /b 1
