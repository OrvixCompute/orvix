"""Daily hot-wallet sweep entrypoint (invoked by the systemd timer).

Run: python scripts/sweep_hot.py  (from /opt/orvix/orchestrator, .env in CWD)

Sweeps USDC above HOT_SWEEP_THRESHOLD_USDC from the hot wallet to cold main.
The transfer is stubbed unless TREASURY_SWEEP_STUB=false. Exits non-zero on error.
"""

import asyncio
import sys


def main() -> int:
    # Imported here so the module stays importable in tests without app env.
    from app.database import get_supabase
    from app.services.hot_sweeper import hot_sweeper

    try:
        result = asyncio.run(hot_sweeper.run_once(get_supabase()))
    except Exception as exc:  # noqa: BLE001
        print(f"hot-sweep FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"hot-sweep: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
