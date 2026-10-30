@echo off
REM Double-click me to add a "Pokemon TCG Tracker" shortcut with the app logo
REM to your Desktop and Start Menu (then right-click it -> Pin to taskbar).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1"
pause
