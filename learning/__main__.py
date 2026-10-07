"""One shadow cycle when the Research Desk flag file is present."""

from __future__ import annotations

from learning.runtime import FLAG_PATH
from learning.shadow import run_cycle


def main() -> None:
    if not FLAG_PATH.is_file():
        print("shadow mode is off")
        return
    recorded = run_cycle(enabled=True)
    print(f"shadow cycle recorded {recorded}")


if __name__ == "__main__":
    main()
