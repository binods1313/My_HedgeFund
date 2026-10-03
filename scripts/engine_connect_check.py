"""Read-only check: does the engine gateway connect to the real terminal?

Touches nothing. Opens the connection, reads account and positions, and closes.
Places no orders.

Run: .venv\\Scripts\\python.exe scripts\\engine_connect_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from engine.config import load_engine_config, read_mt5_credentials
from engine.gateway import Gateway
from engine.journal import Journal


def main() -> int:
    print("=" * 70)
    print("  ENGINE GATEWAY CONNECTIVITY CHECK (read-only, no orders)")
    print("=" * 70)

    config = load_engine_config(root=_REPO_ROOT)
    creds = read_mt5_credentials()
    print(f"  config mode   : {config.mode}")
    print(f"  login present : {creds['login'] is not None}")
    print(f"  server        : {creds['server'] or '(unset)'}")
    print()

    db = _REPO_ROOT / "data" / "engine_connect_check.db"
    journal = Journal(db)
    gateway = Gateway(
        login=creds["login"],
        password=creds["password"],
        server=creds["server"],
        terminal_path=creds["terminal_path"] or None,
        magic=config.engine.magic_number,
        call_timeout=config.engine.gateway.call_timeout_seconds,
        journal=journal,
    )
    gateway.start()

    try:
        if not gateway.ensure_connected():
            print(f"  [FAIL] could not connect: {gateway.status.last_error}")
            return 1

        print(f"  [OK] connected on gateway thread "
              f"{gateway._thread.ident}")
        print()

        try:
            account = gateway.account_info()
        except Exception as exc:
            print(f"  [FAIL] account_info: {type(exc).__name__}: {exc}")
            return 1

        if account is None:
            print("  [FAIL] account_info() returned None")
            return 1

        currency = getattr(account, "currency", "?")
        print(f"  [OK] account  : {getattr(account, 'server', '?')}")
        print(f"       login    : ***{str(account.login)[-3:]}")
        print(f"       currency : {currency}")
        print(f"       balance  : {account.balance:,.2f}")
        print(f"       equity   : {account.equity:,.2f}")
        print(f"       margin   : {account.margin:,.2f}")
        print()

        try:
            positions = gateway.positions_get()
        except Exception as exc:
            print(f"  [FAIL] positions_get: {type(exc).__name__}: {exc}")
            return 1

        print(f"  [OK] positions_get returned {len(positions)} position(s)")
        magic = config.engine.magic_number
        ours = 0
        for pos in positions:
            belongs = getattr(pos, "magic", None) == magic
            if belongs:
                ours += 1
            print(f"       {'OURS  ' if belongs else 'other '} "
                  f"ticket={pos.ticket} {pos.symbol} vol={pos.volume} "
                  f"magic={pos.magic}")
        print()
        print(f"  positions with our magic {magic}: {ours}")
        print("  (the engine adopts these on startup; it places nothing)")

        print()
        print("  RESULT: gateway connects and reads broker state successfully.")
        return 0
    finally:
        gateway.shutdown_terminal()
        gateway.stop()
        journal.close()
        db.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())