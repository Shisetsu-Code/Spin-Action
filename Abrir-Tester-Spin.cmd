@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creando entorno virtual...
  py -3 -m venv .venv
  if errorlevel 1 goto :error
)

echo Actualizando dependencias...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :error

if not exist ".venv\.chromium-ready" (
  echo Instalando Chromium de Playwright...
  ".venv\Scripts\python.exe" -m playwright install chromium
  if errorlevel 1 goto :error
  type nul > ".venv\.chromium-ready"
)

echo Abriendo Tester-Spin...
".venv\Scripts\pythonw.exe" run.py
exit /b 0

:error
echo.
echo ERROR preparando Tester-Spin.
pause
exit /b 1
