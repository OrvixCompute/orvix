# Payment Flow — Devnet Test & Mainnet Activation

The end-to-end guide for taking the payment flow live: what the pieces are, how to
prove them on devnet, and how to switch on real mainnet money **progressively** with
a fast kill switch. Companion to [treasury.md](treasury.md) (wallet architecture) and
[payouts.md](payouts.md) (withdrawal semantics).

Everything ships **disabled by default** (`ENABLE_PAYMENT_LISTENER=false`,
`PAYOUT_STUB=true`, `ENABLE_PAYOUT_WORKER=false`, `TREASURY_SWEEP_STUB=true`). Nothing
below moves real funds until you flip a flag.

---

## 1. The moving parts

| Component | Code | Flag(s) | What it does |
|-----------|------|---------|--------------|
| Payment listener | `services/payment_listener.py` | `ENABLE_PAYMENT_LISTENER` | Polls the **hot** wallet, matches deposit memo → `topup_intents`, credits via `credit_topup` (idempotent on signature). |
| Payout worker | `services/payout_service.py` | `ENABLE_PAYOUT_WORKER`, `PAYOUT_STUB` | Settles `queued` withdrawals: real SPL send from **payout** wallet → provider, confirm, settle/refund. |
| Hot sweeper | `services/hot_sweeper.py` | `ENABLE_HOT_SWEEPER`, `TREASURY_SWEEP_STUB` | Daily hot → main sweep of the excess above `HOT_SWEEP_THRESHOLD_USDC`. |
| Monitoring | `GET /v1/admin/payments/overview` · `scripts/payment_status.py` | — | Read-only snapshot: flags, balances, deposits, withdrawal queue. |

---

## 2. Prerequisites (do these first — for BOTH devnet and mainnet)

The treasury wallets must exist and be provisioned before any of this can sign a
transaction. Per [treasury.md](treasury.md):

1. `python scripts/generate_treasury_wallets.py` (locally) → `main`, `hot`, `payout`.
2. `main` private key → paper, delete from disk. `hot` + `payout` → `scp` to VPS, `chmod 600`.
3. `.env`: set `TREASURY_WALLET_ADDRESS` (=hot pubkey), `TREASURY_MAIN_PUBLIC`,
   `PAYOUT_WALLET_PUBLIC`, `TREASURY_KEYPAIR_PATH`, `PAYOUT_KEYPAIR_PATH`,
   `USDC_MINT_ADDRESS`, `HELIUS_RPC_URL` + `HELIUS_API_KEY`.
4. `python scripts/create_treasury_atas.py` — creates the hot + payout USDC ATAs.
5. Fund the **payout** wallet from **main** (offline-signed) with a small float.

> For **mainnet** use the seeded migration `013_treasury_wallets` and fill the real
> pubkeys. For **devnet** use throwaway wallets and a devnet USDC mint (below).

---

## 3. Devnet test phase

Prove every path against a live devnet before touching mainnet money. The harness
(`scripts/devnet_e2e.py`) broadcasts real devnet transactions and then runs the
actual listener / payout / sweeper code against them.

### 3.1 Devnet environment

Use a **separate devnet Supabase project** (or a schema copy) — never the prod DB.
Apply all migrations (`001`–`013`) to it.

Point a devnet `.env` (or a throwaway checkout) at:

```
SUPABASE_URL=<devnet supabase>
SUPABASE_SERVICE_KEY=<devnet key>
HELIUS_RPC_URL=https://devnet.helius-rpc.com
HELIUS_API_KEY=<devnet key>
USDC_MINT_ADDRESS=<devnet USDC mint>      # e.g. Circle devnet USDC: 4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU
# small limits so test amounts are valid:
MIN_WITHDRAW_AMOUNT_USDC=0.1
AUTO_APPROVE_MAX_USDC=100
# treasury: devnet throwaway wallets + keypair paths (as in §2)
```

Fund the wallets on devnet:
- `solana airdrop 2 <wallet> --url devnet` for SOL (fees/rent) on hot, payout, source.
- Get devnet USDC from <https://faucet.circle.com/> into the **source** wallet.

Harness env vars:

```
E2E_SOURCE_KEYPAIR_PATH=/path/to/devnet-source.json   # funded (SOL + USDC), acts as the user
E2E_PROVIDER_WALLET=<pubkey>            # payout destination owner (default: source owner)
E2E_DEPOSIT_USDC=1.0                    # scenario A
E2E_WITHDRAW_USDC=0.5                   # scenario B/E (>= MIN_WITHDRAW, < AUTO_APPROVE)
```

### 3.2 Run the scenarios

```bash
# dry plan (no broadcast):
python scripts/devnet_e2e.py --all

# scenario A only, real send (needs ENABLE_* / *_STUB set for that path):
python scripts/devnet_e2e.py --scenario a --yes

# full run with real payout + sweep:
PAYOUT_STUB=false TREASURY_SWEEP_STUB=false python scripts/devnet_e2e.py --all --yes
```

| Scenario | Proves | Needs |
|----------|--------|-------|
| **A** top-up detection | deposit+memo → listener credits the user, intent → fulfilled | source has devnet USDC |
| **B** payout send+confirm | queued withdrawal → real USDC to provider, tx confirmed, row `completed` | `PAYOUT_STUB=false`, payout wallet funded |
| **D** hot sweep | hot over threshold → real hot→main transfer, main grows | `TREASURY_SWEEP_STUB=false`, `TREASURY_MAIN_PUBLIC` set |
| **E** failure→refund | forced pre-broadcast failure → withdrawal `failed`, balance refunded | (deterministic; moves no funds) |

The harness prints a report table with **tx signatures** — verify each on
<https://explorer.solana.com/?cluster=devnet>. Exit code is non-zero if any scenario
is not PASS/SKIP.

> Scenario A drives `payment_listener._process_signature` directly, so it validates
> the parse+credit path without a separately-running listener. To also test the live
> polling loop, run the orchestrator against devnet with `ENABLE_PAYMENT_LISTENER=true`
> and watch `scripts/payment_status.py`.

---

## 4. Progressive mainnet activation

Only after **all devnet scenarios pass**. One lever at a time; monitor between steps.

**Backup first:** rsync `/opt/orvix/orchestrator` to a timestamped backup and copy `.env`.

### Step 1 — Payment listener only (detect deposits, no outflow)
```
ENABLE_PAYMENT_LISTENER=true
# keep: PAYOUT_STUB=true, ENABLE_PAYOUT_WORKER=false, TREASURY_SWEEP_STUB=true
```
Restart. Watch `scripts/payment_status.py` for 24h. Send yourself a tiny real top-up
(with the intent memo) and confirm it credits. **No funds leave** in this step.

### Step 2 — Real payout, capped
```
PAYOUT_STUB=false
ENABLE_PAYOUT_WORKER=true
MIN_WITHDRAW_AMOUNT_USDC=<high, e.g. 5>   # small blast radius at first
```
Keep the payout wallet funded with only a small float. Wait for a real withdrawal,
verify the signature on mainnet explorer, monitor 48h. Watch for rows stuck in
`processing` (needs_review in the dashboard — see [payouts.md](payouts.md)).

### Step 3 — Hot sweeper
```
TREASURY_SWEEP_STUB=false
ENABLE_HOT_SWEEPER=true
```
Confirm the `orvix-hot-sweeper.timer` is enabled. Verify the first sweep lands in main.

`BUYBACK_STUB` / `BURN_STUB` stay **true** throughout — buyback/burn are Phase 2.

---

## 5. Kill switch (target < 5 min)

If anything looks wrong, revert the relevant lever and restart:

```
# stop all outflow immediately:
PAYOUT_STUB=true
ENABLE_PAYOUT_WORKER=false
TREASURY_SWEEP_STUB=true
# stop crediting too, if needed:
ENABLE_PAYMENT_LISTENER=false
```
```bash
systemctl restart orvix-orchestrator
python scripts/payment_status.py    # confirm flags flipped
```
The listener is idempotent (credit is guarded by the unique signature), and the payout
worker only picks `queued` rows — so restarting never double-credits or double-sends.

---

## 6. Monitoring & maintenance

- **Dashboard:** `python scripts/payment_status.py [--sync] [--json]` on the VPS, or
  `GET /v1/admin/payments/overview` (X-Admin-Key) for the frontend/ops.
- **Refresh balances:** `POST /v1/admin/treasury/sync` (or `--sync`) reads on-chain.
- **Stuck payouts:** anything in `needs_review` (status `processing`) — reconcile the
  signature on-chain, then settle/refund manually per [payouts.md](payouts.md).
- **Unattributed deposits:** deposits with no memo / no matching intent are warn-logged
  only (`grep 'Unattributed deposit'`), not persisted.
- **Weekly:** top up the payout wallet from main as it drains.
- **Known gap:** the listener cursor is in-memory; on a >25-deposit downtime older
  deposits can be missed until a later deposit re-triggers a scan (idempotent, not a
  double-credit risk). Persisting the cursor is a tracked follow-up.
