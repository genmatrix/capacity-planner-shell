@echo off
rem ---------------------------------------------------------------
rem Capacity Planner launcher — double-click to run.
rem First run: builds a private Python environment on THIS machine
rem (fast local imports; the share only serves the app + team state).
rem Later runs: starts the app in a few seconds.
rem Re-syncs packages automatically whenever requirements.txt changes.
rem ---------------------------------------------------------------
setlocal
rem pushd maps a UNC share to a drive letter so relative paths
rem (scenarios/, data_paths.json) resolve to the SHARED copies.
pushd "%~dp0" || (echo Cannot access the app folder. & pause & exit /b 1)

set "VENVROOT=%LOCALAPPDATA%\CapacityPlanner"
set "VENV=%VENVROOT%\venv"
set "PYEXE=%VENV%\Scripts\python.exe"

where py >nul 2>nul
if errorlevel 1 (
  echo Python is not installed on this machine.
  echo Install Python 3.11+ from Software Center or python.org
  echo ^(the default per-user install needs no admin rights^), then
  echo double-click this launcher again.
  pause & exit /b 1
)

set NEEDS_INSTALL=
if not exist "%PYEXE%" set NEEDS_INSTALL=1
fc /b requirements.txt "%VENVROOT%\requirements.installed" >nul 2>nul
if errorlevel 1 set NEEDS_INSTALL=1

if defined NEEDS_INSTALL (
  echo First-time setup ^(or an update^) — this takes a few minutes once...
  if not exist "%PYEXE%" py -3 -m venv "%VENV%"
  if not exist "%PYEXE%" (echo Could not create the environment. & pause & exit /b 1)
  if exist wheels (
    rem Offline install from the share's wheels\ folder — no internet needed.
    "%PYEXE%" -m pip install --no-index --find-links wheels -r requirements.txt
  ) else (
    "%PYEXE%" -m pip install -r requirements.txt
  )
  if errorlevel 1 (
    echo Package install failed. If this machine has no internet access,
    echo ask for the wheels\ folder to be populated ^(see TEAM_SETUP.md^).
    pause & exit /b 1
  )
  copy /y requirements.txt "%VENVROOT%\requirements.installed" >nul
)

"%PYEXE%" -m streamlit run capacity_planner.py
popd
pause
