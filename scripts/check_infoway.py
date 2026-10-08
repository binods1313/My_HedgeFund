"""One Infoway quote. Prints a documented failure class and no response body."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ui.services.infoway_client import smoke_report


def main() -> None:
    report = smoke_report()
    present = "yes" if report["key_present"] else "no"
    print(f"key_present={present}")
    print(f"http_status={report['http_status']}")
    print(f"ret={report['ret']}")
    print(f"error_code={report['error_code']}")
    print(f"failure_class={report['failure_class']}")
    print(f"latency_ms={report['latency_ms']}")
    print(report["result"])


if __name__ == "__main__":
    main()
