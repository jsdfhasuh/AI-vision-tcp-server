@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if exist ".venv-build\Scripts\python.exe" goto build
where py >nul 2>&1
if not errorlevel 1 (
    py -3.12 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        py -3.12 -m venv .venv-build
        goto checkenv
    )
    py -3 -m venv .venv-build
    goto checkenv
)
python -m venv .venv-build
:checkenv
if not exist ".venv-build\Scripts\python.exe" goto failed
:build
".venv-build\Scripts\python.exe" -m pip install --upgrade pip pyinstaller
if errorlevel 1 goto failed
".venv-build\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --onedir --name VisionCompetitionServer --add-data "competition\web:competition/web" server.py
if errorlevel 1 goto failed
copy /y README.md "dist\VisionCompetitionServer\README.md" >nul
xcopy /e /i /y docs "dist\VisionCompetitionServer\docs" >nul
echo.
echo Build complete: dist\VisionCompetitionServer\VisionCompetitionServer.exe
echo Copy the ENTIRE VisionCompetitionServer folder, including _internal.
echo Default data: %%LOCALAPPDATA%%\VisionCompetitionTCP\data
pause
exit /b 0
:failed
echo.
echo Build failed. Review the errors above. Build the Windows EXE on Windows.
pause
exit /b 1
