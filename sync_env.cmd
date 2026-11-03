@echo off
setlocal
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\sync-env.py" %*
set "SYNC_ENV_RESULT=%errorlevel%"
if "%~1"=="" pause
exit /b %SYNC_ENV_RESULT%
