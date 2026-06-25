# Frequently Asked Questions

## General

**What is Orvix?**
Orvix is a decentralized GPU inference network on Solana. It lets developers access AI inference through an OpenAI-compatible API, paid for in ORVX, while letting GPU owners earn by serving requests.

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
Per token (input + output), with discounts based on your ORVX holding tier (up to 25% off for Diamond tier).

## For Providers

**What hardware do I need?**
At minimum: NVIDIA GPU with 8GB VRAM, CUDA 11+. Recommended: 16GB+ VRAM (RTX 3090/4090, A4000+).

**How much can I earn?**
Providers receive 70% of the revenue from requests they serve. Actual earnings depend on uptime, model demand, and number of competing nodes.

**How are payouts handled?**
Earnings accumulate in your account. You can withdraw to your Solana wallet anytime above the minimum threshold.

## Token

**Where can I get ORVX?**
ORVX is launched on pump.fun. Check the official Twitter/Discord for the verified token mint address. Never trust unverified sources.

**Is ORVX a security?**
Orvix is a utility token used for paying for compute, staking nodes, and governance participation. Nothing in our docs constitutes investment advice or guarantees of returns.

## Project

**When is mainnet?**
We're currently in alpha development. Public testnet is planned after closed alpha validation. See [CHANGELOG.md](../CHANGELOG.md) for current status.

**Is the code open source?**
Yes, under Apache 2.0. See [LICENSE](../LICENSE).

**How can I contribute?**
See [CONTRIBUTING.md](../CONTRIBUTING.md). All types of contributions welcome.

## More questions?

Open a [GitHub Discussion](https://github.com/OrvixCompute/orvix/discussions) or reach out on [Discord](https://discord.gg/orvix) (link coming soon).
