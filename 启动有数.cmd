@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_product.ps1" -Interactive -OpenBrowser %*
if errorlevel 1 (
  echo Startup failed. Please check runtime/competition_v1/server.err.log.
  pause
  exit /b 1
)
