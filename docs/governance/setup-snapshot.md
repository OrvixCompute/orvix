# Snapshot Space Setup (admin)

This is a one-time, browser-based configuration. Do it with the admin wallet.

## 1. Create the space

1. Go to https://snapshot.box (or https://snapshot.org).
2. Connect the admin **Phantom** wallet.
3. **Create new space** (ENS not required — use the Solana strategy).
4. Space details:
   - **Name:** Orvix
   - **Symbol:** ORVX
   - **Categories:** AI, Infrastructure
   - **Description:** from the whitepaper
   - **Avatar:** Orvix logo (`.github/assets/logo.svg`)
   - **Network:** Solana

## 2. Voting strategy

Use the **SPL Token Balance** strategy:

- **Network:** Solana mainnet
- **Token address:** `<ORVX_MINT_ADDRESS>`
- **Decimals:** `<verify from the token>` (matches `ORVX_DECIMALS`)
- **Min balance to create a proposal:** 100,000 ORVX
- **Min balance to vote:** any positive holding

## 3. Voting settings

- **Voting type:** single-choice (simple) or weighted (complex)
- **Voting period:** 5 days
- **Voting delay:** 1 day
- **Quorum:** 5,000,000 ORVX minimum participation (raise as supply burns)

## 4. Admins

- Admin wallet + 1 backup (use a multisig if available).

## 5. Wire the backend

Set in `.env` so `/v1/governance/snapshot-url` returns the live space:

```
GOVERNANCE_SNAPSHOT_SPACE=orvix
GOVERNANCE_SNAPSHOT_URL=https://snapshot.box/#/orvix
```

## 6. Verify

- The space is reachable at the URL.
- You can create a test proposal as admin.
- You can vote with ORVX held in a test wallet, and the weight matches the
  holding amount.
