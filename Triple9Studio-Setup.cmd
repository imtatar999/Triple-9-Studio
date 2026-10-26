@echo off
rem Triple 9 Studio - one-click setup: downloads the program from GitHub and installs everything it needs.
rem Optional: Triple9Studio-Setup.cmd "D:\Folder\For\The Program"
setlocal
set "BOOT=%TEMP%\t9-bootstrap.ps1"
if exist "%~dp0SYNCED-LYRICS-DOWNLOADER-MAIN\setup\bootstrap.ps1" (
  copy /y "%~dp0SYNCED-LYRICS-DOWNLOADER-MAIN\setup\bootstrap.ps1" "%BOOT%" >nul
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/imtatar999/Triple-9-Studio/main/SYNCED-LYRICS-DOWNLOADER-MAIN/setup/bootstrap.ps1' -OutFile '%BOOT%'"
  if errorlevel 1 (
    echo Could not download the setup script. Check your internet connection.
    pause
    exit /b 1
  )
)
if "%~1"=="" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%BOOT%"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%BOOT%" -Target "%~1"
)
if errorlevel 1 pause
