@echo off
rem Creates a private Python environment in .venv (only psutil is installed there).
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" exit /b 0
set "PY="
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=python"
if not defined PY py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=py -3"
if not defined PY goto :fail
echo [rmq] first start: creating .venv and installing psutil...
%PY% -m venv .venv || goto :fail
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :fail
exit /b 0
:fail
echo [rmq] could not prepare the environment: Python 3.10 or newer is needed (python.org, "Add python.exe to PATH").
pause
exit /b 1
