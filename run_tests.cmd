@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
where py >nul 2>&1
if not errorlevel 1 (
    py -3.12 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        py -3.12 -m unittest discover -s tests -v
        goto finish
    )
    py -3 -m unittest discover -s tests -v
    goto finish
)
python -m unittest discover -s tests -v
:finish
pause
endlocal
