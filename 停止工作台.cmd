@echo off
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" "%~dp0tools\launcher.py" stop
if errorlevel 1 pause
