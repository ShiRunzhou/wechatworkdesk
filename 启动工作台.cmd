@echo off
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" "%~dp0tools\launcher.py" start
if errorlevel 1 pause
