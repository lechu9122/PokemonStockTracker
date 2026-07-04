@echo off
REM ---------------------------------------------------------------------------
REM run.bat - one-command launcher for the Pokemon TCG Tracker (Windows).
REM   run.bat        : set up (if needed) and start the app on port 8501
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "VENV_DIR=.venv"
if "%PORT%"=="" set "PORT=8501"

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

REM 3. Launch the app.
echo Starting Pokemon TCG Tracker on http://localhost:%PORT% (Ctrl+C to stop) ...
streamlit run app.py --server.port %PORT%
goto :eof

:error
echo Setup failed. See the message above.
exit /b 1
