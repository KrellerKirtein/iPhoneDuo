@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-web.ps1" %*
set "startup_exit=%ERRORLEVEL%"
if not "%startup_exit%"=="0" (
  echo.
  echo Startup failed. Review the message above.
  pause
)
exit /b %startup_exit%
