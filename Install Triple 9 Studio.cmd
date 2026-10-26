@echo off
rem Double-click to install (or repair) everything Triple 9 Studio needs. See SYNCED-LYRICS-DOWNLOADER-MAIN\setup\install.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0SYNCED-LYRICS-DOWNLOADER-MAIN\setup\install.ps1" %*
if errorlevel 1 exit /b 1
