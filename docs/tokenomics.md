# Tokenomics

> 📝 Tokenomics may evolve based on community input. Last updated: 2026-06-24.
>
> Nothing in this document is investment advice or a guarantee of returns. ORVX
> is a utility token. See the [FAQ](./faq.md) for more.

## Overview

ORVX is the utility token that powers the Orvix network.

- **Total supply:** 1,000,000,000 ORVX (fixed)
- **Launch:** pump.fun

> Always verify the official token mint address via the project's verified
> accounts — [@Orvixhq](https://x.com/Orvixhq) on X or
> [t.me/Orvix_hq](https://t.me/Orvix_hq) on Telegram. Never trust unverified sources.

> 💵 **Payments are settled in USDC, not ORVX.** Developers fund their balance
> and pay for inference in USDC, and providers are paid out in USDC. ORVX is the
> network's *utility* token — it powers discounts, staking, and governance
> (below), not the unit of account for compute.

## Utility

### Compute payment discounts
Inference is billed and settled in **USDC**. **Staking** ORVX unlocks tiered
pricing discounts (see the tiers below) — the more you stake, the lower your
per-token cost. Tier is derived from your staked balance, not your wallet
balance. Billing logic is in place today; real on-chain settlement is being
finalized alongside live inference.

### Node staking
Providers must stake at least **2,000,000 ORVX** — **0.2% of the fixed
1,000,000,000 supply** — to register and run nodes. Staking is **custodial
(off-chain ledger) in v1**, moving **on-chain in v2**. Stake is deposited via a
memo'd transfer to the treasury and credited automatically.

### User staking (non-custodial)
Users can lock ORVX directly in the Orvix staking program (`staking-program/`,
an Anchor v2 program) for a chosen lock period (3 / 7 / 14 days). Tokens sit in
a program-owned vault that no operator key can move; unstaking is only possible
after the lock expires. Tier is derived from the on-chain `StakeAccount` PDA,
so staking for utility works without trusting the operator with custody.
Opt-in via `USER_STAKING_PROGRAM_ID`.

The figure is set while the token is early and the stake is inexpensive in
dollar terms, and is expected to be revisited as market cap grows — it is a
configuration value (`PROVIDER_MIN_STAKE_ORVX`), not a protocol constant. Two
consequences are worth stating plainly: a per-provider minimum of 0.2% means the
supply can seat at most 500 providers even if every token were staked, and since
the barrier is denominated in ORVX rather than dollars, its real cost moves with
the price without anyone changing the setting.

> ⚠️ **Stake requirement currently suspended for the alpha phase**
> (`REQUIRE_STAKE_FOR_PROVIDER=false`). Providers can register without staking
> for now; the requirement activates before public testnet. See
> [provider-guide.md](./provider-guide.md#current-status-alpha) for the migration plan.

### Premium access tiers
Tiers are **stake-based**: your tier is determined by how much ORVX you have
staked, not how much you hold.

| Tier    | Staked ORVX        | Discount |
|---------|--------------------|----------|
| Bronze  | 0 – 9,999          | 0%       |
| Silver  | 10,000 – 49,999    | 5%       |
| Gold    | 50,000 – 249,999   | 15%      |
| Diamond | 250,000+           | 25%      |

### Buyback & burn
The platform takes a **30% fee** on compute revenue (providers keep 70%). That
fee is split **50% buyback / 30% treasury / 20% operations**. The buyback budget
is used to buy ORVX from the open market (via Jupiter), and the bought-back ORVX
is **burned monthly** by sending it to the Solana incinerator address
`1nc1nerator11111111111111111111111111111111`. Every buyback and burn is on-chain
and publicly verifiable; see the transparency endpoints under `/v1/staking/`.

> More AI usage → more USDC revenue → more ORVX bought from the market → more
> burned → lower circulating supply.

#### Revenue flow

```
Customer pays in USDC
        │
        ├── 70% → Provider (USDC)
        └── 30% → Platform fee
                   ├── 50% → Buyback budget → buy ORVX → hold
                   ├── 30% → Treasury
                   └── 20% → Operations
                                   │
                          (monthly) burn held ORVX → incinerator
```

#### Transparency

Every buyback and burn is on-chain and publicly verifiable:

- `GET /v1/staking/buyback-history` — buybacks with Solana signatures
- `GET /v1/staking/burn-history` — burns with Solana signatures
- `GET /v1/staking/network-stats` — totals staked, bought, held, and burned

### Governance
Governance is **off-chain in v1**, moving **on-chain in v2**, giving the
community a say in network parameters and direction.

## Status

The token economy is early and subject to change as the network moves from
alpha toward public testnet. See [CHANGELOG.md](../CHANGELOG.md) for current
project status.
