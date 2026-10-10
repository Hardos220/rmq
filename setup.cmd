@echo off
rem Creates a private Python environment in .venv (only psutil is installed there).
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" exit /b 0
echo [rmq] first start: creating .venv and installing psutil...
python -m venv .venv || goto :fail
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :fail
exit /b 0
:fail
echo [rmq] could not prepare the environment. Python 3.10+ must be on PATH.
pause
exit /b 1
