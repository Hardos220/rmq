@echo off
if exist "%~dp0..\.venv\Scripts\python.exe" (
    "%~dp0..\.venv\Scripts\python.exe" "%~dp0..\rmq_cli.py" %*
) else (
    python "%~dp0..\rmq_cli.py" %*
)
exit /b %ERRORLEVEL%
