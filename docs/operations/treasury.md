# Treasury Architecture & Operations

Orvix uses a **3-wallet cold/hot/payout** treasury split so a hot-key compromise
caps the loss to a small operational balance. (A separate buyback wallet is Phase 2.)

| Role | Key location | Purpose |
|------|--------------|---------|
| **main** | OFFLINE (paper / hardware) — public key only on server | Cold storage; holds the bulk of funds |
| **hot** | `TREASURY_KEYPAIR_PATH` on VPS (chmod 600) | Receives incoming USDC deposits; the **payment listener subscribes to this address** (`TREASURY_WALLET_ADDRESS`) |
| **payout** | `PAYOUT_KEYPAIR_PATH` on VPS (chmod 600) | Signs provider payouts (Session 3) |

Money flow: users deposit → **hot** → daily sweep of the excess → **main** (cold).
Payouts are funded **main → payout** manually (offline-signed), then payout → providers.

> `TREASURY_WALLET_ADDRESS` **is** the hot wallet. Do not repoint it — the listener
> depends on it.

## One-time setup

1. **Generate keypairs (locally, never on the VPS):**
   ```bash
   python scripts/generate_treasury_wallets.py   # writes treasury-keys/{main,hot,payout}.json
   ```
2. **Secure the keys:**
   - `main` → copy the byte array to **paper**, then **delete** `treasury-keys/main.json`.
   - `hot`, `payout` → `scp` to the VPS, `chmod 600`.
3. **Configure `.env`** on the VPS:
   ```
   TREASURY_WALLET_ADDRESS=<hot pubkey>      # already the listener's target
   TREASURY_KEYPAIR_PATH=/opt/orvix/secrets/hot.json
   TREASURY_MAIN_PUBLIC=<main pubkey>
   PAYOUT_WALLET_PUBLIC=<payout pubkey>
   PAYOUT_KEYPAIR_PATH=/opt/orvix/secrets/payout.json
   ```
4. **Apply migration** `013_treasury_wallets.sql` (Supabase SQL Editor), then fill in the
   `public_key` column for each role (or call `POST /v1/admin/treasury/sync`).
5. **Create USDC ATAs** (mandatory — SPL transfers to a wallet with no ATA fail):
   ```bash
   python scripts/create_treasury_atas.py   # idempotent; hot pays rent for hot + payout ATAs
   ```

## Daily sweeper

`scripts/sweep_hot.py` (run by the `orvix-hot-sweeper.timer`, 00:30 UTC) sweeps
`hot_balance − HOT_SWEEP_MIN_KEEP_USDC` to **main** whenever the hot balance exceeds
`HOT_SWEEP_THRESHOLD_USDC`.

```bash
cp scripts/systemd/orvix-hot-sweeper.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now orvix-hot-sweeper.timer
```

The actual transfer is **stubbed** (`TREASURY_SWEEP_STUB=true`) until vetted on devnet
(Session 4). Flip to `false` only after a successful devnet sweep.

## Admin endpoints (X-Admin-Key)

- `GET  /v1/admin/treasury/balances` — last-synced balances from the DB.
- `POST /v1/admin/treasury/sync` — refresh on-chain balances into `treasury_wallets`.
- `POST /v1/admin/treasury/sweep-hot` — trigger a sweep now (respects the stub flag).

## Kill switch / rotation

- **Disable sweeps:** `TREASURY_SWEEP_STUB=true` (or stop the timer) + restart.
- **Hot key compromised:** generate a new hot keypair, update `TREASURY_WALLET_ADDRESS`
  + `TREASURY_KEYPAIR_PATH`, restart, create its ATA, and re-point the payment listener
  (it already reads `TREASURY_WALLET_ADDRESS`). Move any funds off the old hot wallet.
- **main** key never touches the server; a server compromise cannot drain cold storage.

## Notes / limitations
- Single-signer hot/payout wallets (mitigated by small balances). Squads multisig for
  `main` is a future improvement.
- Real sends across the treasury are gated by stub flags and validated on devnet before
  mainnet activation.
