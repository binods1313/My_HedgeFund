"""Update the MT5 account in .env without echoing any value.

Prints only which keys changed and whether each is set. The password is read
from the existing file if already present, so it never has to appear in a
command line or in output.

Usage:
    python scripts/set_mt5_account.py --login 123 --server "Broker-Server"
    python scripts/set_mt5_account.py --password-stdin   # reads from stdin
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"

# Keys this script owns.
MANAGED = ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_env(path: Path, values: dict[str, str]) -> None:
    order = list(values.keys())
    lines = ["# MT5 terminal account",
             "# Credentials live here and are gitignored. Never commit this file.",
             "# If this value is ever pasted into chat or a log, rotate it.",
             ""]
    for key in order:
        lines.append(f"{key}={values[key]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login")
    parser.add_argument("--server")
    parser.add_argument("--path", dest="terminal_path")
    parser.add_argument("--password-stdin", action="store_true",
                        help="read the password from stdin")
    args = parser.parse_args(argv)

    values = read_env(ENV)
    before = dict(values)

    if args.login:
        values["MT5_LOGIN"] = args.login.strip()
    if args.server:
        values["MT5_SERVER"] = args.server.strip()
    if args.terminal_path:
        values["MT5_PATH"] = args.terminal_path.strip()

    if args.password_stdin:
        supplied = sys.stdin.readline().rstrip("\n").rstrip("\r")
        values["MT5_PASSWORD"] = supplied
    elif "MT5_PASSWORD" not in values:
        # Prompt without echo.
        values["MT5_PASSWORD"] = getpass.getpass("MT5 password: ")

    for key in ("OPENROUTER_API_KEY", "XAI_API_KEY", "GROQ_API_KEY",
                "TELEGRAM_BOT_TOKEN", "SMTP_PASSWORD"):
        if key in before and key not in values:
            values[key] = before[key]

    write_env(ENV, values)

    print(f"updated {ENV}")
    print()
    print("  key            changed   set")
    print("  " + "-" * 34)
    for key in MANAGED:
        if key not in values:
            print(f"  {key:14s} {'-':9s} no")
            continue
        changed = "yes" if values.get(key) != before.get(key) else "no"
        present = "yes" if values.get(key) else "no"
        print(f"  {key:14s} {changed:9s} {present}")
    print()
    print("  No value was printed. Verify with:")
    print("    python scripts/check_env_keys.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())