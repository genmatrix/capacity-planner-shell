@echo off
rem ---------------------------------------------------------------
rem Capacity Planner — LOCAL COPY TEST launcher (opt-in, 2026-10-28).
rem
rem Same app, same shared plan. The difference: the app's CODE runs from
rem a copy on THIS PC (%LOCALAPPDATA%\CapacityPlanner\app) instead of
rem from the share. The plan, the edit locks, drafts and the team
rem settings (data_paths.json, holidays.json, field_maps.json,
rem acd_maps.json, branding.json) all stay HERE on the share, exactly as
rem with the normal launcher — CP_TEAM_DIR below points the app at them.
rem
rem Why: every click re-runs the app's main script, and Streamlit checks
rem that script's file about 7 times per run. On the share each check is
rem a network round trip; on a local copy it is free. Compare the sidebar
rem timing line against the normal launcher.
rem
rem The normal "Launch Capacity Planner.bat" is unchanged. Run it once
rem first on a new PC (it builds the Python environment), and again
rem whenever this one says the packages changed.
rem ---------------------------------------------------------------
setlocal
pushd "%~dp0" || (echo Cannot access the app folder. & pause & exit /b 1)
set "CP_TEAM_DIR=%CD%"

set "VENVROOT=%LOCALAPPDATA%\CapacityPlanner"
set "PYEXE=%VENVROOT%\venv\Scripts\python.exe"
set "LOCALAPP=%VENVROOT%\app"

if not exist "%PYEXE%" (
  echo The Python environment is not set up on this PC yet.
  echo Double-click "Launch Capacity Planner.bat" once, close it, then try again.
  pause & exit /b 1
)
fc /b requirements.txt "%VENVROOT%\requirements.installed" >nul 2>nul
if errorlevel 1 (
  echo The app's packages changed since this PC last set them up.
  echo Double-click "Launch Capacity Planner.bat" once, close it, then try again.
  pause & exit /b 1
)

rem Copy ONLY the code. robocopy skips files that have not changed, so a
rem normal launch copies nothing; after an update it copies the new files.
rem Exit codes 0-7 are success; 8 or more is a failure.
robocopy "%CD%\." "%LOCALAPP%" capacity_planner.py collab.py sources.py brand.py data_io.py /NJH /NJS /NFL /NDL /NP >nul
if errorlevel 8 (
  echo Could not copy the app to this PC. Use the normal launcher instead.
  pause & exit /b 1
)

rem The working folder stays on the share (pushd above), so Streamlit still
rem reads the share's .streamlit\config.toml, and CP_TEAM_DIR keeps every
rem piece of team state on the share. Only the code runs locally.
"%PYEXE%" -m streamlit run "%LOCALAPP%\capacity_planner.py" --server.fileWatcherType=none
popd
pause
