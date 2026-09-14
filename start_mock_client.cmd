@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
where py >nul 2>&1
if not errorlevel 1 (
    py -3.12 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        py -3.12 client_example.py %*
        goto finish
    )
    py -3 client_example.py %*
    goto finish
)
python client_example.py %*
:finish
pause
endlocal
