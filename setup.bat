@echo off
setlocal enabledelayedexpansion

echo ========================================================
echo  Autonomous AI Hedge Fund - Environment Setup
echo ========================================================

cd /d "%~dp0"

REM Check if Python is installed
python --version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python is not installed or not added to PATH.
    echo Please install Python (3.12 - 3.14) and ensure "Add Python to PATH" is checked.
    pause
    exit /b 1
)

echo [1/3] Checking / Creating virtual environment (.venv)...
if not exist ".venv\Scripts\python.exe" (
    echo Creating .venv with python -m venv .venv ...
    python -m venv .venv
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo Virtual environment created successfully.
) else (
    echo Virtual environment .venv already exists.
)

echo [2/3] Upgrading pip and installing dependencies from requirements.txt...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Dependency installation failed.
    pause
    exit /b 1
)

echo [3/3] Verifying environment...
".venv\Scripts\python.exe" -c "import MetaTrader5, pandas, groq, streamlit; print('[OK] All required modules verified!')"
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Module verification failed.
    pause
    exit /b 1
)

echo ========================================================
echo  Setup Completed Successfully!
echo  You can now run: run.bat
echo ========================================================
pause
