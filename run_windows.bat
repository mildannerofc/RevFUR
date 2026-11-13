@echo off
setlocal
cd /d "%~dp0"
set "PY_LAUNCHER="
for %%V in (3.13 3.12 3.11 3.10) do call :check_python %%V
if not defined PY_LAUNCHER (
    echo ERROR: Python 3.10 or newer was not found.
    echo Install Python 3.10+ from https://www.python.org/downloads/windows/
    pause
    exit /b 1
)
echo Using %PY_LAUNCHER%
%PY_LAUNCHER% -m pip install -r requirements.txt
if errorlevel 1 (
    echo Optional drag-and-drop dependency could not be installed.
    echo The Browse button will still work if tkinter is available.
)
%PY_LAUNCHER% main.py %*
if errorlevel 1 pause
endlocal
exit /b 0
:check_python
if defined PY_LAUNCHER exit /b 0
py -%1 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY_LAUNCHER=py -%1"
exit /b 0
