"""Treasury balance monitor (invoked by the systemd timer, or by hand).

Run: python scripts/treasury_health.py [--json]
     (from /opt/orvix/orchestrator, with .env in the working directory)

Exits 1 when any threshold is breached, so systemd records a failed unit and
`systemctl list-units --failed` surfaces it without anyone reading logs.
Exits 2 when the check itself could not run — an RPC outage is not the same as
a healthy treasury, and must not be reported as one.
"""

import asyncio
import json
import sys
from pathlib import Path

# `python scripts/foo.py` puts scripts/ on sys.path, not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    from app.services.treasury_health import check

    as_json = "--json" in sys.argv

    try:
        result = asyncio.run(check())
    except Exception as exc:  # noqa: BLE001
        # Distinct exit code: "could not check" must never look like "all fine".
        print(f"treasury-health COULD NOT RUN: {exc}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps(result, indent=2))
    else:
        for role, w in result["wallets"].items():
            if w["public_key"] is None:
                print(f"{role:7} not configured")
                continue
            print(f"{role:7} {w['sol']:.6f} SOL   {w['usdc']:.6f} USDC   {w['public_key']}")
        if result["ok"]:
            print("\ntreasury-health: OK")
        else:
            print()
            for a in result["alerts"]:
                print(f"{a['severity'].upper():8} {a['wallet']}/{a['asset']}: {a['message']}")
                print(f"         balance {a['balance']} < floor {a['threshold']}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
