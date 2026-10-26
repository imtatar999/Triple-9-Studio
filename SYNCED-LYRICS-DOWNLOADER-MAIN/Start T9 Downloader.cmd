@echo off
setlocal
rem The program's own Python (made by "Install Triple 9 Studio.cmd"), else the older Python 3.10 setup.
set "PYW=%~dp0..\runtime\app\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
if not exist "%PYW%" set "PYW=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not exist "%PYW%" (
  echo Triple 9 Studio is not installed yet.
  echo Double-click "Install Triple 9 Studio.cmd" in the main program folder first.
  pause
  exit /b 1
)
start "" "%PYW%" "%~dp0triplenine_lyrics_downloader.py"
