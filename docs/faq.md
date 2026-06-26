# Frequently Asked Questions

## General

**What is Orvix?**
Orvix is a decentralized GPU inference network on Solana. It lets developers access AI inference through an OpenAI-compatible API, paid for in USDC, while letting GPU owners earn USDC by serving requests.

**How is Orvix different from OpenAI?**
Orvix routes requests to a distributed network of GPU providers rather than centralized servers. This means lower cost, no vendor lock-in, and earning opportunities for hardware owners.

**How is Orvix different from io.net or Render?**
Orvix focuses specifically on AI inference (not general GPU compute or rendering), uses an OpenAI-compatible API for drop-in integration, and is built natively on Solana for low transaction fees.

## For Developers

**How do I use the Orvix API?**
Get an API key from the dashboard, then use any OpenAI-compatible client by setting `base_url` to the Orvix endpoint. See [API Reference](./api-reference.md).

**What models are supported?**
Initially: Llama 3.1 8B, Mistral 7B, Qwen 2.5 7B. More open-source models added based on community demand.

**How is pricing calculated?**
Per token (input + output), with discounts based on your **staked** ORVX tier (up to 25% off for Diamond). Tier is determined by how much ORVX you have staked, not how much you hold. See [Tokenomics](./tokenomics.md#premium-access-tiers).

## For Providers

**What hardware do I need?**
At minimum: NVIDIA GPU with 8GB VRAM, CUDA 11+. Recommended: 16GB+ VRAM (RTX 3090/4090, A4000+).

**What do I need to become a provider?**
Eligible hardware **and** a stake of at least **25,000 ORVX**. Registration is rejected until the stake is in place. See the [Provider Guide](./provider-guide.md#provider-requirements).

**How much can I earn?**
Providers receive 70% of the revenue from requests they serve (the platform takes 30%). Actual earnings depend on uptime, model demand, and number of competing nodes.

**How are payouts handled?**
Earnings accumulate in your account in USDC. You can withdraw to your Solana wallet anytime above the minimum threshold. Your staked ORVX is separate and is returned via unstaking.

## Token

**Where can I get ORVX?**
ORVX is launched on pump.fun. Check the official Twitter/Discord for the verified token mint address. Never trust unverified sources.

**Is ORVX a security?**
ORVX is a utility token used for pricing-tier discounts, staking nodes, and governance participation — compute itself is paid for in USDC. Nothing in our docs constitutes investment advice or guarantees of returns.

## Staking, buybacks & burns

**How does staking work?**
You stake ORVX by sending it (with a memo from `POST /v1/staking/stake-intent`) to the treasury, which credits your staked balance. Staking does two things: it makes you eligible to run a provider node (25,000 ORVX minimum) and it sets your pricing tier. Staking is custodial in v1 and moving on-chain in v2.

**What's the difference between staking and holding ORVX?**
Holding is just having ORVX in your wallet. Staking locks ORVX into custody to unlock utility — provider eligibility and tier discounts are based on **staked** ORVX, not what's in your wallet.

**How can I unstake my ORVX?**
Call `POST /v1/staking/unstake`. The amount is debited and a payout is queued to your wallet. Providers cannot unstake below the 25,000 ORVX minimum without deregistering first.

**When are buybacks executed?**
50% of the platform fee accumulates as a buyback budget. Buybacks are executed manually by the team (USDC→ORVX via Jupiter). See `GET /v1/staking/buyback-history`.

**When is the next burn?**
Bought-back ORVX is burned **monthly** (first week, for the previous month). See `GET /v1/staking/burn-history`.

**Where can I see proof of burns?**
Every burn sends ORVX to the Solana incinerator and is verifiable on Solscan. The signatures are listed at `GET /v1/staking/burn-history`.

**How do I participate in governance?**
Any ORVX holder can vote on [Snapshot](https://snapshot.box/#/orvix) (gasless, weighted by holdings); 100,000+ ORVX can create proposals. See the [Governance docs](./governance/README.md).

## Project

**When is mainnet?**
We're currently in alpha development. Public testnet is planned after closed alpha validation. See [CHANGELOG.md](../CHANGELOG.md) for current status.

**Is the code open source?**
Yes, under Apache 2.0. See [LICENSE](../LICENSE).

**How can I contribute?**
See [CONTRIBUTING.md](../CONTRIBUTING.md). All types of contributions welcome.

## More questions?

Open a [GitHub Discussion](https://github.com/OrvixCompute/orvix/discussions) or reach out on [Discord](https://discord.gg/orvix) (link coming soon).
