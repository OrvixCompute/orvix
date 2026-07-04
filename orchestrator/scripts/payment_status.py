"""Payment-flow status dashboard (CLI).

Prints the same snapshot as GET /v1/admin/payments/overview — payment flags,
treasury balances, 24h deposits, and withdrawal queue state — straight from the
DB, no admin key needed. Use it on the VPS during progressive activation.

Run (from /opt/orvix/orchestrator, .env in CWD):
  python scripts/payment_status.py            # read last-synced balances
  python scripts/payment_status.py --sync      # refresh on-chain balances first
  python scripts/payment_status.py --json       # machine-readable
"""

import asyncio
import json
import sys


def _fmt_flags(flags: dict) -> str:
    on = lambda b: "ON " if b else "off"  # noqa: E731
    lines = [
        f"  payment_listener : {on(flags['enable_payment_listener'])}",
        f"  payout_worker    : {on(flags['enable_payout_worker'])}",
        f"  payout_stub      : {'STUB' if flags['payout_stub'] else 'REAL'}",
        f"  sweep_stub       : {'STUB' if flags['treasury_sweep_stub'] else 'REAL'}",
        f"  hot_sweeper      : {on(flags['enable_hot_sweeper'])}",
        f"  min_withdraw     : {flags['min_withdraw_amount_usdc']} USDC",
        f"  auto_approve_max : {flags['auto_approve_max_usdc']} USDC",
        f"  usdc_mint        : {'set' if flags['usdc_mint_configured'] else 'UNSET'}",
        f"  orvx_mint        : {'set' if flags['orvx_mint_configured'] else 'unset'}",
    ]
    return "\n".join(lines)


def _fmt_treasury(rows: list[dict]) -> str:
    if not rows:
        return "  (no treasury_wallets rows)"
    out = []
    for r in sorted(rows, key=lambda x: x.get("wallet_role", "")):
        pub = r.get("public_key") or "(unset)"
        usdc = r.get("balance_usdc")
        synced = r.get("balance_last_synced_at") or "never"
        usdc_s = f"{float(usdc):.6f}" if usdc is not None else "?"
        out.append(f"  {r.get('wallet_role',''):<7} {usdc_s:>14} USDC  {pub[:12]}…  synced={synced}")
    return "\n".join(out)


def _fmt_withdrawals(w: dict) -> str:
    lines = [
        f"  queued={w['queued']}  processing={w['processing']}  "
        f"completed_24h={w['completed_24h']}  failed_24h={w['failed_24h']}",
    ]
    review = w.get("needs_review") or []
    if review:
        lines.append(f"  ⚠ {len(review)} withdrawal(s) stuck in 'processing' (manual review):")
        for r in review:
            lines.append(
                f"      {str(r['id'])[:8]}  {r['amount']} USDC  sig={r.get('solana_signature')}  "
                f"{r.get('error_message') or ''}"
            )
    return "\n".join(lines)


def _print_report(data: dict) -> None:
    print("=" * 68)
    print(f"ORVIX PAYMENT FLOW — {data['generated_at']}")
    print("=" * 68)
    print("\nFLAGS")
    print(_fmt_flags(data["flags"]))
    print("\nTREASURY (last-synced balances)")
    print(_fmt_treasury(data["treasury"]))
    d = data["deposits"]
    print("\nDEPOSITS (USDC top-ups)")
    print(f"  last 24h: {d['count_24h']} deposits, {d['total_usdc_24h']} USDC")
    print("\nWITHDRAWALS (provider payouts)")
    print(_fmt_withdrawals(data["withdrawals"]))
    print()


def main() -> int:
    from app.database import get_supabase
    from app.services.payments_overview import build_overview

    args = set(sys.argv[1:])
    db = get_supabase()

    if "--sync" in args:
        from app.services.wallet import wallet_service

        try:
            asyncio.run(wallet_service.sync_balances(db))
        except Exception as exc:  # noqa: BLE001 — still show DB state on RPC failure
            print(f"(balance sync failed, showing last-synced: {exc})", file=sys.stderr)

    data = build_overview(db)
    if "--json" in args:
        print(json.dumps(data, indent=2, default=str))
    else:
        _print_report(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
