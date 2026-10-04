@echo off
rem 처음 한 번만 인터넷이 필요합니다 (라이브러리 설치). 만들어진 exe는 오프라인에서 동작합니다.
chcp 65001 > nul
cd /d %~dp0
if not exist .venv (
    python -m venv .venv || goto :error
    .venv\Scripts\python -m pip install -r requirements.txt || goto :error
)
.venv\Scripts\python -m pytest -q || goto :error
.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed ^
    --name PdfPriceEditor ^
    --exclude-module tkinter --exclude-module pytest ^
    --workpath "%TEMP%\pdfprice_build" --specpath "%TEMP%\pdfprice_build" ^
    --distpath dist ^
    main.py || goto :error
echo.
echo 완료: dist\PdfPriceEditor.exe
exit /b 0
:error
echo 빌드 실패
exit /b 1
