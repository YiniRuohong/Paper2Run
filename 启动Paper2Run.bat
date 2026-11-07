@echo off
rem Paper2Run one-click launcher (Windows): double-click.
chcp 65001 >nul
cd /d "%~dp0"
rem Experiments need Docker Desktop (WSL2 backend, Linux containers); the app starts without it.
echo Paper2Run: experiments need Docker Desktop with the WSL2 backend in Linux-container mode.
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 scripts\launch.py
  goto :done
)
where python >nul 2>nul
if %errorlevel%==0 (
  python scripts\launch.py
  goto :done
)
echo [!] Python 3.11+ not found. Install from https://www.python.org/downloads/
echo     and tick "Add python.exe to PATH" during setup.
:done
pause
