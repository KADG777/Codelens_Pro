@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1" %*
set "FLOW_EXIT=%ERRORLEVEL%"
if /i "%~1"=="-CheckOnly" exit /b %FLOW_EXIT%
if not "%FLOW_EXIT%"=="0" pause
exit /b %FLOW_EXIT%
