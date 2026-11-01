@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_product.ps1" -Action Stop %*
if errorlevel 1 (
  echo Stop failed. The verified service may still be handling a request.
  pause
  exit /b 1
)
