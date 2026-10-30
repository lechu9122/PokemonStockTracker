@echo off
REM ---------------------------------------------------------------------------
REM run.bat - one-command launcher for the Pokemon TCG Tracker (Windows).
REM   run.bat        : set up (if needed) and start the app on port 8501
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "VENV_DIR=.venv"
if "%PORT%"=="" set "PORT=8501"
set "URL=http://localhost:%PORT%"

REM 0. Already running (e.g. clicked the taskbar icon twice)? Just open it.
netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul
if not errorlevel 1 (
  start "" "%URL%"
  exit /b 0
)

REM 1. Create the virtualenv the first time.
if not exist "%VENV_DIR%" (
  echo Creating virtualenv in %VENV_DIR% ...
  python -m venv "%VENV_DIR%" || goto :error
)

call "%VENV_DIR%\Scripts\activate.bat"

REM 2. Install dependencies (first run stamps .venv\.deps-installed).
if not exist "%VENV_DIR%\.deps-installed" (
  echo Installing dependencies ...
  python -m pip install --upgrade pip >nul
  python -m pip install -r requirements.txt || goto :error
  echo done> "%VENV_DIR%\.deps-installed"
)

REM 3. Open the browser as soon as the server answers, then launch the app.
REM    Headless mode skips Streamlit's first-run email prompt, which would
REM    otherwise hang silently when started from the minimized shortcut.
start "" /b powershell -NoProfile -Command ^
  "for ($i = 0; $i -lt 120; $i++) { try { Invoke-WebRequest -UseBasicParsing '%URL%/_stcore/health' | Out-Null; Start-Process '%URL%'; break } catch { Start-Sleep -Seconds 1 } }"

echo Starting Pokemon TCG Tracker on %URL% (Ctrl+C to stop) ...
streamlit run app.py --server.port %PORT% --server.headless true
goto :eof

:error
echo Setup failed. See the message above.
exit /b 1
