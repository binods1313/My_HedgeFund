@echo off
setlocal

cd /d "%~dp0"

echo ========================================================
echo  Launching Autonomous AI Hedge Fund Terminal
echo ========================================================

if not exist ".venv\Scripts\python.exe" (
    echo [INFO] Virtual environment .venv not found. Running setup first...
    call setup.bat
    if not exist ".venv\Scripts\python.exe" (
        echo [ERROR] Setup failed to create .venv.
        pause
        exit /b 1
    )
)

echo Starting Streamlit App...
".venv\Scripts\python.exe" -m streamlit run app.py --server.port 8502 --server.headless true

if %ERRORLEVEL% neq 0 (
    echo [ERROR] Streamlit exited with an error.
    pause
)
