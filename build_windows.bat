@echo off
setlocal
cd /d "%~dp0"
set "PY_LAUNCHER="
for %%V in (3.13 3.12 3.11 3.10) do call :check_python %%V
if not defined PY_LAUNCHER (
  echo Python 3.10+ is required.
  exit /b 1
)
%PY_LAUNCHER% -m pip install --upgrade pip pyinstaller tkinterdnd2
if errorlevel 1 exit /b 1
%PY_LAUNCHER% -m PyInstaller --noconfirm --clean --onefile --windowed --name RevFUR --collect-all tkinterdnd2 main.py
if errorlevel 1 exit /b 1
echo Build complete: dist\RevFUR.exe
pause
exit /b 0
:check_python
if defined PY_LAUNCHER exit /b 0
py -%1 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY_LAUNCHER=py -%1"
exit /b 0
