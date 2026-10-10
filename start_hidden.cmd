@echo off
rem Starts the resource manager in the background (no window). Stop it with stop.cmd.
cd /d "%~dp0"
call "%~dp0setup.cmd" || exit /b 1
start "" ".venv\Scripts\pythonw.exe" rmq_cli.py serve
