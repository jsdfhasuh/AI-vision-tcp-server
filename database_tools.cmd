@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
where py >nul 2>&1
if not errorlevel 1 (
    py -3.12 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        py -3.12 database_tools.py --data-dir "%~dp0data" %*
        goto finish
    )
    py -3 database_tools.py --data-dir "%~dp0data" %*
    goto finish
)
python database_tools.py --data-dir "%~dp0data" %*
:finish
if errorlevel 1 (
    echo.
    echo Database operation failed. Read the error above. Existing data is not overwritten by restore.
    pause
)
endlocal
