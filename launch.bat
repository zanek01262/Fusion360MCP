@echo off
REM launch.bat - Windows wrapper for the Fusion 360 MCP preflight helper.
REM
REM   launch.bat          full preflight check (default)
REM   launch.bat ping     just ping the Fusion listener
REM   launch.bat serve    run the server standalone (debugging)
REM
REM Double-clicking runs the full check and pauses so you can read it.

setlocal
set "HERE=%~dp0"

REM Find a python launcher: prefer the 'py' launcher, then python.
REM "if not errorlevel 1" is evaluated when the line RUNS. The old
REM %ERRORLEVEL% test sat inside a ( ) block, where it is expanded when the
REM block is PARSED - so it still held the failed 'where py' result and
REM reported "no python found" even when python was on PATH.
set "PY="
where py >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>&1
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo FAIL: no python found on PATH. Install Python 3.10+ from python.org
  pause
  exit /b 1
)

set "ARG=%~1"
if "%ARG%"=="" set "ARG=check"

%PY% "%HERE%launch.py" %ARG%
set "RC=%ERRORLEVEL%"

REM Pause only when double-clicked (no args) so the window stays open.
if "%~1"=="" pause
endlocal & exit /b %RC%
