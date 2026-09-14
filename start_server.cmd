@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
where py >nul 2>&1
if not errorlevel 1 (
    py -3.12 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        py -3.12 server.py --data-dir "%~dp0data" %*
        goto finish
    )
    py -3 server.py --data-dir "%~dp0data" %*
    goto finish
)
python server.py --data-dir "%~dp0data" %*
:finish
if errorlevel 1 (
    echo.
    echo Startup failed. Install Python 3.10+ with Tcl/Tk and check the error above.
    pause
)
endlocal
