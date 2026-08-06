# Provider Payouts (Operations)

Providers **claim** earnings via a withdrawal request (`POST /v1/provider/withdraw`);
a background worker settles the queue. Orvix does **not** auto-push payouts.

## Flow

1. `queue_withdrawal` moves `available → pending` atomically (`lock_withdrawal`),
   enforces the min amount + daily limit, and flags amounts over
   `AUTO_APPROVE_MAX_USDC` for manual approval.
2. The payout worker (`ENABLE_PAYOUT_WORKER=true`) picks up `queued` rows and, per row:
   - **Broadcast** (`_send_payout`): sends USDC from the **payout** wallet to the
     provider's `destination_wallet`, creating the destination USDC ATA in the same
     transaction if missing (`ensure_dest_ata`). Stubbed unless `PAYOUT_STUB=false`.
   - **Confirm** (`_confirm`): polls the signature up to
     `PAYOUT_CONFIRM_MAX_ATTEMPTS × 2s` for `confirmed`/`finalized`.

## Status semantics (safety)

| Outcome | Row status | Balance | Notes |
|---------|-----------|---------|-------|
| Pre-broadcast failure (bad key/config, RPC rejects submit) | `failed` | **refunded** | No funds moved → safe to refund. |
| Broadcast + confirmed | `completed` | settled (pending cleared) | Signature recorded; ledger tx confirmed. |
| Broadcast + **unconfirmed** after timeout | stays `processing` | **locked, NOT refunded** | Funds may have moved — **never auto-refund** (double-spend guard). Left for manual reconciliation. |

Because the worker only ever picks up `queued` rows, a row stuck in `processing`
is **never re-sent** automatically. An operator verifies the recorded
`solana_signature` on-chain and then either marks it `completed` (settle) or, if the
tx truly never landed, refunds manually.

## Setup / enabling

1. Treasury (Session 2): payout wallet keypair on the VPS (`PAYOUT_KEYPAIR_PATH`,
   chmod 600), `PAYOUT_WALLET_PUBLIC` set, and its USDC ATA created
   (`scripts/create_treasury_atas.py`).
2. **Fund the payout wallet** from cold `main` — a manual, offline-signed transfer
   (main's key never touches the server). Keep only a working balance there.
3. Verify on **devnet** (Session 4) with `PAYOUT_STUB=false` before mainnet.
4. Mainnet activation: `PAYOUT_STUB=false` + `ENABLE_PAYOUT_WORKER=true`. Kill switch:
   set `PAYOUT_STUB=true` (or `ENABLE_PAYOUT_WORKER=false`) + restart.

Flip one variable per step and confirm it took effect before the next. The settings
below are `.env`-only and read at request time, so the way to verify is
`GET /v1/admin/feature-flags` (needs `ADMIN_API_KEY` set), or, if admin is disabled,
on the box:

```bash
cd /opt/orvix/orchestrator && ./.venv/bin/python -c \
  "from app.config import settings; print(settings.MIN_WITHDRAW_AMOUNT_USDC)"
```

pydantic-settings reads `.env` from the working directory at startup, so **a restart is
what picks up a change** — editing `.env` alone does nothing.

## Costs and the withdrawal floor
Each payout is a single SPL transfer (~5000 lamports, ~$0.001) plus a one-time ATA
rent (~0.002 SOL, roughly $0.30–0.50) the first time a provider receives USDC.

`MIN_WITHDRAW_AMOUNT_USDC` exists to keep those costs from dominating the payout, and
the two costs pull in different directions:

- The **per-transfer fee** is negligible against almost any amount — it is ~10% of a
  1¢ withdrawal, but rounding error above ~$1.
- The **one-time ATA rent** is the real floor. For a provider's first-ever payout the
  treasury pays ~$0.30–0.50 regardless of size, so a first withdrawal much below that
  costs Orvix more than it moves.

There is no single right value; pick it from the economics above once real provider
earnings exist. A low or zero floor is a deliberate alpha choice — it keeps small-scale
testing unblocked and is safe while `PAYOUT_STUB=true`, since nothing is sent on-chain.
`amount` is validated `> 0` by the request model regardless, so a floor of `0` still
rejects zero and negative requests. Revisit the floor **before** flipping
`PAYOUT_STUB=false`, because that is the point where the rent starts being real money.

`AUTO_APPROVE_MAX_USDC` and `MAX_WITHDRAWALS_PER_DAY` are the other two levers: the
first caps what settles without a human, the second caps per-user frequency. Both are
reported by `GET /v1/admin/feature-flags` alongside the floor.
