@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set "FLOW_EXIT=%ERRORLEVEL%"
if /i "%~1"=="-CheckOnly" exit /b %FLOW_EXIT%
pause
exit /b %FLOW_EXIT%
