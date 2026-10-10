@echo off
rem Stops the manager. Running tasks keep going and are picked up on the next start.
cd /d "%~dp0"
".venv\Scripts\python.exe" rmq_cli.py stop
