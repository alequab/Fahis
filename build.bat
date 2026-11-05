@echo off
rem Builds the Windows installer into the dist folder. Needs the Setup steps from the README first.
rem   build         reuses what hasn't changed, so repeat builds are quick
rem   build clean   starts from nothing, for a final release
rem   build test    makes dist\win-unpacked only, the fastest way to try a change
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" -c "import PyInstaller" 2>nul || ".venv\Scripts\python.exe" -m pip install --quiet -r requirements-build.txt || exit /b 1
set "CLEAN="
if /i "%~1"=="clean" set "CLEAN=--clean"
rem Only the profile files meant for release are listed, so a developer's downloads stay out of it.
".venv\Scripts\python.exe" -m PyInstaller --noconfirm %CLEAN% --console --name fahis-backend ^
  --distpath build\backend --workpath build\work --specpath build ^
  --icon "%CD%\electron\icon.ico" ^
  --add-data "%CD%\data;data" ^
  --add-data "%CD%\profiles\catalog.json;profiles" ^
  --add-data "%CD%\profiles\kia-picanto.json;profiles" ^
  --add-data "%CD%\profiles\LICENSE;profiles" ^
  --add-data "%CD%\electron\index.html;electron" ^
  --collect-all bleak --collect-all winrt ^
  obd_tool.py || exit /b 1
if /i "%~1"=="test" (call npm run dist:test) else (call npm run dist)
if errorlevel 1 exit /b 1
