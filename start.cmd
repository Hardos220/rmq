@echo off
rem Starts the resource manager in this window and opens the dashboard.
rem Closing the window stops the manager only: running tasks keep going and are
rem picked up again on the next start.
cd /d "%~dp0"
call "%~dp0setup.cmd" || exit /b 1
".venv\Scripts\python.exe" rmq_cli.py serve --open %*
if errorlevel 1 pause
