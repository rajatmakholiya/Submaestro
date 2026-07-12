@echo off
REM ===================================================================
REM  Sourcer launcher - double-click to run the whole app.
REM  Everything installs into the local .venv folder next to this file,
REM  so nothing is ever installed system-wide on your machine.
REM ===================================================================
setlocal
cd /d "%~dp0"

set "VENV=%~dp0.venv"
set "PY=%VENV%\Scripts\python.exe"
set "STAMP=%VENV%\.deps-installed"

REM --- Create the virtual environment if it doesn't exist yet ---------
if not exist "%PY%" (
    echo [Sourcer] First run: creating virtual environment in .venv ...
    where py >nul 2>&1
    if %errorlevel%==0 (
        py -3 -m venv "%VENV%"
    ) else (
        python -m venv "%VENV%"
    )
    if not exist "%PY%" (
        echo.
        echo [Sourcer] ERROR: could not create the virtual environment.
        echo           Make sure Python 3 is installed and on your PATH.
        echo.
        pause
        exit /b 1
    )
)

REM --- Install/upgrade dependencies only when needed ------------------
REM  Re-runs pip only if the stamp is missing or requirements.txt changed.
set "NEED_INSTALL="
if not exist "%STAMP%" (
    set "NEED_INSTALL=1"
) else (
    for /f %%A in ('dir /b /o-d "%STAMP%" "%~dp0requirements.txt" 2^>nul') do (
        if /i "%%A"=="requirements.txt" set "NEED_INSTALL=1"
        goto :checkeddeps
    )
)
:checkeddeps
if defined NEED_INSTALL (
    echo [Sourcer] Installing dependencies into .venv ^(one-time^) ...
    "%PY%" -m pip install --upgrade pip >nul
    "%PY%" -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo.
        echo [Sourcer] ERROR: dependency installation failed. See messages above.
        echo.
        pause
        exit /b 1
    )
    echo done> "%STAMP%"
)

REM --- Warn if ffmpeg isn't available (needed for trim/merge/mp3) -----
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo.
    echo [Sourcer] WARNING: ffmpeg was not found on your PATH.
    echo           Trimming, MP3 conversion, and quality merging need it.
    echo           Install it from https://www.gyan.dev/ffmpeg/builds/ and add to PATH.
    echo.
)

REM --- Launch: open the browser, then run the server -----------------
echo [Sourcer] Starting server at http://127.0.0.1:8765
REM Open the browser a couple seconds later, once the server is up.
start "" cmd /c "timeout /t 3 /nobreak >nul & start "" http://127.0.0.1:8765"
echo [Sourcer] Close this window (or press Ctrl+C) to stop the app.
echo.
"%PY%" "%~dp0app.py"

endlocal
