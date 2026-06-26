# Admin scripts — buyback & burn

These are standalone CLIs run on the VPS as the orchestrator service user. They
read the same `.env` as the app and share the service logic and guardrails used
by the `/v1/admin/*` endpoints.

> **Safety:** real on-chain actions are gated behind stub flags. `BUYBACK_STUB`
> and `BURN_STUB` default to `true` — the swap/transfer is simulated and a fake
> signature is returned. Flip them to `false` only after devnet testing and once
> the treasury keypair is configured (`TREASURY_KEYPAIR_PATH`, `chmod 600`).

## buyback.py

```bash
python scripts/buyback.py status
python scripts/buyback.py preview --amount-usdc 100 --slippage-bps 50
python scripts/buyback.py execute --amount-usdc 100 --slippage-bps 50      # prompts y/N
python scripts/buyback.py execute --amount-usdc 100 --yes                  # no prompt
```

Guardrails: amount must be > 0 and ≤ the accumulated `buyback_budget_usdc`; the
Jupiter price impact must be ≤ `BUYBACK_MAX_SLIPPAGE_BPS`; at most one buyback
per `BUYBACK_MIN_INTERVAL_SECONDS`. The DB row is written only after the swap is
confirmed; if recording fails after a confirmed swap it is logged loudly for
manual reconciliation.

## burn.py

```bash
python scripts/burn.py status
python scripts/burn.py execute                                   # burns ALL held, prev month
python scripts/burn.py execute --amount 5000 --period-start 2026-05-01 --period-end 2026-06-01
```

Burns send ORVX to the incinerator (`INCINERATOR_ADDRESS`,
`1nc1nerator11111111111111111111111111111111`). Guardrails: amount ≤
`orvx_held_for_burn`; `period_end > period_start` and not in the future; refuses
a signature already present in `burn_events`.

## Test plan (do this before any real execution)

1. **Devnet first.** Point `HELIUS_RPC_URL` at devnet, fund a test treasury with
   devnet USDC, set devnet `USDC_MINT_ADDRESS` / `ORVX_MINT_ADDRESS`.
2. `python scripts/buyback.py preview --amount-usdc 1` — confirm a quote returns.
3. Set `BUYBACK_STUB=false`, then `execute --amount-usdc 1` — confirm the swap
   lands and a `buyback_events` row is created.
4. `python scripts/burn.py execute --amount 1` with `BURN_STUB=false` — verify on
   Solscan and confirm `burn_events` matches on-chain.
5. **Mainnet smoke test** with the smallest possible amount, verify on Solscan,
   then scale up.

## Configuration (.env)

`ADMIN_API_KEY`, `ORVX_MINT_ADDRESS`, `USDC_MINT_ADDRESS`, `ORVX_DECIMALS`,
`USDC_DECIMALS`, `BUYBACK_STUB`, `BURN_STUB`, `BUYBACK_MAX_SLIPPAGE_BPS`,
`BUYBACK_MIN_INTERVAL_SECONDS`, `JUPITER_QUOTE_API`, `INCINERATOR_ADDRESS`,
`TREASURY_KEYPAIR_PATH`, `HELIUS_RPC_URL`, `AUDIT_LOG_DIR`.
