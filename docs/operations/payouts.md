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
4. Mainnet activation: `PAYOUT_STUB=false` + `ENABLE_PAYOUT_WORKER=true`, ideally with
   a higher `MIN_WITHDRAW_AMOUNT_USDC` at first. Kill switch: set `PAYOUT_STUB=true`
   (or `ENABLE_PAYOUT_WORKER=false`) + restart.

## Costs
Each payout is a single SPL transfer (~5000 lamports, ~$0.001) plus a one-time ATA
rent (~0.002 SOL) the first time a provider receives USDC.
