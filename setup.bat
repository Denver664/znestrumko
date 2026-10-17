@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo   Znestrumko 2.0 - setup
echo ========================================
echo.

where py >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON=py -3"
) else (
    where python >nul 2>&1
    if not %errorlevel%==0 (
        echo Python was not found. Install Python 3.10+ and try again.
        pause
        exit /b 1
    )
    set "PYTHON=python"
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PYTHON% -m venv .venv
    if errorlevel 1 goto :error
) else (
    echo Virtual environment already exists.
)

echo Installing and updating libraries...
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :error
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo.
echo Setup completed successfully.
echo You can now run start.bat
pause
exit /b 0

:error
echo.
echo Setup failed. Check the error above.
pause
exit /b 1
