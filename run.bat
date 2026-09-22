@echo off
rem Double-click to start bandobuddy. First run sets up a private Python environment.
setlocal
cd /d "%~dp0"
title bandobuddy

if exist ".venv\Scripts\python.exe" goto :deps

echo Setting up bandobuddy for the first time...
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul || goto :nopython
%PY% -m venv .venv || goto :nopython

:deps
rem (Re)install dependencies whenever requirements.txt has changed since the last install.
fc /b requirements.txt ".venv\installed-requirements.txt" >nul 2>nul && goto :run
echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :installfailed
copy /y requirements.txt ".venv\installed-requirements.txt" >nul

:run
".venv\Scripts\python.exe" -m bandobuddy %*
if errorlevel 1 pause
exit /b

:nopython
echo.
echo bandobuddy needs Python 3.9 or newer.
echo Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this again.
pause
exit /b 1

:installfailed
echo.
echo Couldn't install dependencies. Check your internet connection and run this again.
pause
exit /b 1
