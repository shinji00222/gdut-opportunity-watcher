@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$s = Get-Content -LiteralPath '%~dp0install_tasks.ps1' -Raw -Encoding UTF8; Invoke-Expression $s"
if errorlevel 1 pause
