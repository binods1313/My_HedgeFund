# Configuration — all secrets loaded from .env (never hardcoded here)
# Copy .env.example to .env and fill in your values.
import os
from dotenv import load_dotenv

load_dotenv()

MT5_LOGIN    = int(os.environ.get("MT5_LOGIN", "0"))
MT5_PASSWORD = os.environ.get("MT5_PASSWORD", "")
MT5_SERVER   = os.environ.get("MT5_SERVER", "MetaQuotes-Demo")
MT5_PATH     = os.environ.get("MT5_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL   = os.environ.get("OPENROUTER_MODEL", "openrouter/free")
