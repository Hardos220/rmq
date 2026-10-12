@echo off
rem Installs the resource manager on this PC: a private Python environment (.venv), a desktop shortcut and
rem the rules for installed AI agents; then starts the manager and opens the dashboard.
rem No administrator rights needed. Safe to run again, for example after moving the folder.
chcp 65001 >nul
cd /d "%~dp0"
title Менеджер ресурсов: установка

set "PY="
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=python"
if not defined PY py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=py -3"
if not defined PY goto :nopython

echo [rmq] 1/4 окружение Python и psutil...
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv || goto :fail
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :fail

echo [rmq] 2/4 ярлык на рабочем столе...
".venv\Scripts\python.exe" rmq_cli.py install-shortcut || goto :fail

echo [rmq] 3/4 правила для ИИ-агентов...
".venv\Scripts\python.exe" rmq_cli.py install-agents

echo [rmq] 4/4 запуск менеджера и дашборда...
".venv\Scripts\python.exe" rmq_cli.py launch || goto :fail

echo.
echo Готово. Менеджер работает в фоне, дашборд открыт в браузере.
echo Дальше его открывает ярлык «Менеджер ресурсов» на рабочем столе.
echo.
echo По желанию:
echo  - температура процессора: tools\README.md, нужен один запрос UAC;
echo  - вычислительная сеть с другими ПК: вкладка «Сеть» в дашборде, там пошаговая инструкция
echo    (понадобится Tailscale: https://tailscale.com/download );
echo  - автозапуск при входе в Windows: Win+R, shell:startup, скопируйте туда ярлык.
echo.
pause
exit /b 0

:nopython
echo.
echo Не найден Python 3.10 или новее.
echo Установите его с https://www.python.org/downloads/ и отметьте «Add python.exe to PATH»,
echo затем запустите install.cmd ещё раз.
echo.
pause
exit /b 1

:fail
echo.
echo Установка не удалась, причина — выше. Исправьте её и запустите install.cmd ещё раз.
echo.
pause
exit /b 1
