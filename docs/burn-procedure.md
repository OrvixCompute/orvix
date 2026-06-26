# ORVX Burn Procedure

Bought-back ORVX is burned **monthly** by sending it to the Solana incinerator
(`1nc1nerator11111111111111111111111111111111`). This is a manual, human-confirmed
operation. The reminder timer (`deploy/systemd/orvix-burn-reminder.timer`) nudges
on the 1st of each month but never burns automatically.

## When

First week of each month, covering the **previous** calendar month. If a burn is
skipped, document why publicly — a missed scheduled burn is trust damage.

## Pre-burn checklist

- [ ] No buyback is mid-flight; the last buyback is confirmed on-chain.
- [ ] `python scripts/burn.py status` — note `orvx_held_for_burn`.
- [ ] DB `orvx_held_for_burn` matches the treasury's on-chain ORVX intended for burn.
- [ ] Treasury (burn) wallet has SOL for transaction fees.
- [ ] `BURN_STUB=false` and `TREASURY_KEYPAIR_PATH` point at the correct keypair.

## Execute

```bash
# Burn all held ORVX for the previous calendar month (default period):
python scripts/burn.py execute

# Or a specific amount / period:
python scripts/burn.py execute --amount 5000 \
  --period-start 2026-05-01 --period-end 2026-06-01
```

The script prompts for confirmation, sends the transfer, confirms it, records it
via `record_burn`, and prints a Solscan link.

## Post-burn

- [ ] Verify the transaction on Solscan; confirm the amount and incinerator dest.
- [ ] Confirm `burn_events` and `global_accounting` match the on-chain transfer.
- [ ] Confirm circulating supply decreased on a DEX/explorer.
- [ ] Publish proof publicly.

### Announcement template

> 🔥 Orvix monthly burn — <MONTH YEAR>
> Burned **<AMOUNT> ORVX** bought back from <USDC> of platform revenue.
> Tx: https://solscan.io/tx/<SIGNATURE>
> Running total burned: <TOTAL> ORVX. Verify any time at <buyback/burn history>.

## Exceptions

- **Failed tx:** nothing is recorded (DB write happens only after on-chain
  confirmation). Re-run after diagnosing.
- **Partial burn:** pass an explicit `--amount`; the remainder stays held for the
  next burn.
- **Confirmed on-chain but DB write failed:** the service logs
  `BURN RECONCILIATION NEEDED` with the signature — record it manually so totals
  stay accurate.
