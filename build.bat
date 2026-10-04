@echo off
rem Build dist\PdfPriceEditor.exe
rem Internet is needed only the first time (to install libraries). The exe runs offline.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment and installing libraries...
    python -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
)

echo [2/3] Running tests...
".venv\Scripts\python.exe" -m pytest -q || goto :error

echo [3/3] Building exe...
".venv\Scripts\pyinstaller.exe" --noconfirm --clean --onefile --windowed ^
    --name PdfPriceEditor ^
    --exclude-module tkinter --exclude-module pytest ^
    --workpath "%TEMP%\pdfprice_build" --specpath "%TEMP%\pdfprice_build" ^
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
