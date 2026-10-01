# Autonomous AI Hedge Fund Terminal ⚡

A high-performance algorithmic trading and portfolio management terminal powered by **MetaTrader 5**, **Groq LLaMA-3**, and **Streamlit**.

---

## 📁 Project Architecture

- **`config.py`**: Account credentials, MT5 executable path, and Groq API keys.
- **`data_engine.py`**: MT5 connection initializer and multi-timeframe candle data fetcher (4H trend & 15M entry).
- **`ai_brain.py`**: Senior Portfolio Manager decision engine powered by Groq LLaMA-3 (`BUY`, `SELL`, `HOLD`).
- **`execution.py`**: Smart order routing with dynamic filling mode resolution (`IOC` / `FOK`) on MT5.
- **`app.py`**: Streamlit dashboard with real-time feedback spinners and bold signal indicators.
- **`setup.bat`**: Automated one-click setup script (creates `.venv`, installs dependencies).
- **`run.bat`**: One-click startup script for launching the terminal.
- **`requirements.txt`**: Exact pinned versions of all project dependencies.

---

## 🐍 Python & Dependency Compatibility

- **Python Version**: Tested and verified on **Python 3.12** through **Python 3.14 (3.14.7 x64)**.
- **MetaTrader5 Compatibility**: `metatrader5` version `5.0.6231` is fully compatible with Windows 64-bit under Python 3.14 and Python 3.12.

---

## 🚀 Quick Start Guide

### Option 1: One-Click Setup & Launch (Windows)

1. Double-click **`setup.bat`** to automatically create the virtual environment (`.venv`) and install all required libraries.
2. Configure your credentials in **`config.py`**:
   ```python
   MT5_LOGIN = 12345678
   MT5_PASSWORD = "your_password"
   MT5_SERVER = "your_broker_server"
   MT5_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
   GROQ_API_KEY = "gsk_..."
   ```
3. Double-click **`run.bat`** to launch the terminal.

---

### Option 2: Manual Setup via PowerShell / Command Prompt

1. **Create the virtual environment**:
   ```powershell
   python -m venv .venv
   ```

2. **Activate the virtual environment**:
   - PowerShell:
     ```powershell
     .\.venv\Scripts\Activate.ps1
     ```
   - Command Prompt:
     ```cmd
     .\.venv\Scripts\activate.bat
     ```

3. **Install dependencies**:
   ```powershell
   .\.venv\Scripts\python.exe -m pip install --upgrade pip
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

4. **Launch the Streamlit Terminal**:
   ```powershell
   .\.venv\Scripts\python.exe -m streamlit run app.py
   ```

---

## 🛠️ VS Code Integration

The project includes pre-configured [`.vscode/settings.json`](file:///.vscode/settings.json) and [`.vscode/launch.json`](file:///.vscode/launch.json):
- Points the default interpreter directly to `${workspaceFolder}/.venv/Scripts/python.exe`.
- Provides an **F5** debug configuration (`Run Streamlit App`) to run directly within the editor.
