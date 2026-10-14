@echo off
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 goto pip
uv pip install --python ".venv\Scripts\python.exe" ".[voice]"
goto finish
:pip
".venv\Scripts\python.exe" -m ensurepip
if errorlevel 1 goto finish
".venv\Scripts\python.exe" -m pip install ".[voice]"
:finish
pause
