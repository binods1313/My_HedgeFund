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

echo [1/4] Checking / Creating virtual environment (.venv)...
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

echo [2/4] Upgrading pip and installing dependencies from requirements.txt...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Dependency installation failed.
    pause
    exit /b 1
)

echo [3/4] Verifying environment...
REM Verify the modules the app actually imports at startup, not a hand-picked
REM subset. The previous check omitted yaml and dotenv, so a missing
REM dependency in requirements.txt went unnoticed until the app crashed.
".venv\Scripts\python.exe" -c "import MetaTrader5, pandas, streamlit, pydantic, requests, yaml, dotenv; print('[OK] All required modules verified!')"
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Module verification failed.
    echo         If a module is missing, it is not declared in requirements.txt.
    echo         Run: .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo [4/4] Verifying configuration and application imports...
".venv\Scripts\python.exe" -c "from config_loader import load_config; c=load_config(); print('[OK] config.yaml valid:', len(c.trading.tradable_symbols), 'tradable symbols'); import ai_brain, execution, data_engine, formatting; print('[OK] Application modules import cleanly')"
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Application import check failed.
    echo         This is the check that would have caught the missing PyYAML.
    pause
    exit /b 1
)

echo ========================================================
echo  Setup Completed Successfully!
echo  You can now run: run.bat
echo ========================================================
pause
