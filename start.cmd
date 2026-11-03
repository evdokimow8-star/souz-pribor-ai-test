@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
  "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m pribor
  goto end
)
py -3 --version >nul 2>nul
if not errorlevel 1 (
  py -3 -m pribor
  goto end
)
python --version >nul 2>nul
if not errorlevel 1 (
  python -m pribor
  goto end
)
echo Установите Python 3.11 или новее с python.org.
:end
pause
