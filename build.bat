@echo off
REM ---------------------------------------------------------------------------
REM One-click build.  Double-click this file, or run it from a terminal.
REM
REM Everything real -- the checks, the messages, the packaging -- lives in
REM build.py.  This file only has to find Python, so it stays plain ASCII on
REM purpose: cmd reads a .bat in the OEM codepage, so Chinese text here would
REM come out as garbage both on GitHub and in some consoles.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PY="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto have_python

python -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto have_python

echo.
echo   Python not found.
echo.
echo   Install Python 3.14 or newer from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" while installing.
echo.
pause
exit /b 1

:have_python
%PY% build.py %*
set "CODE=%ERRORLEVEL%"

echo.
pause
exit /b %CODE%
