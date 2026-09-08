@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PROJECT_ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%scripts\windows\start.ps1" %*
exit /b %ERRORLEVEL%
