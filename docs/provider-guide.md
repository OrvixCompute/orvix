# Provider Guide

> 📝 Provider onboarding is in alpha. We're actively refining the experience,
> so steps and requirements may change.

## What is a provider?

A provider is someone who runs a GPU node on the Orvix network. Your machine
connects to the orchestrator, receives inference jobs, runs them on your GPU,
and streams the results back. In return, you earn a share of the revenue from
the requests you serve.

## Current Status (Alpha)

> 🟢 **During the alpha phase, the 25,000 ORVX stake requirement is suspended.**
> Providers can register **without staking** (the orchestrator runs with
> `REQUIRE_STAKE_FOR_PROVIDER=false`). The stake requirement will activate before
> public testnet — early providers will be **grandfathered** or given time to
> acquire and stake ORVX. The stake mechanics below describe the post-alpha model.

## Provider requirements

To run a node you need eligible **hardware** and (post-alpha) a **stake**.

### Hardware

- **GPU:** NVIDIA GPU with **8GB+ VRAM** minimum (16GB+ recommended, e.g.
  RTX 3090 / 4090, A4000 or better).
- **Drivers:** recent NVIDIA driver + CUDA 11 or newer.
- **OS:** Linux preferred.
- **Network:** stable connection with reasonable upload bandwidth.

> No GPU yet? The node ships with a **stub mode** (`ORVIX_NODE_STUB_GPU=true`)
> so you can develop and test the full flow without hardware.

### Stake: 25,000 ORVX

Provider registration requires at least **25,000 ORVX staked**. This aligns
provider incentives with the network.

1. **Acquire ORVX** — on pump.fun or a Solana DEX (verify the mint address from
   official channels).
2. **Stake it** — call `POST /v1/staking/stake-intent` for a memo, then send the
   ORVX to the treasury with that memo. Your stake is credited automatically.
   Check `GET /v1/staking/status`.
3. **Register** — `POST /v1/provider/register` succeeds once your stake ≥ 25,000.

> If you unstake below 25,000 ORVX you must deregister as a provider first — the
> unstake endpoint refuses to drop an active provider below the minimum.

## High-level steps

1. Create an account and register as a provider (`POST /v1/provider/register`),
   which returns your node secret.
2. Install the node agent (see [orvix-node/README.md](../orvix-node/README.md)).
3. Configure the agent with your provider ID and node secret.
4. Start the node: `orvix-node start`.
5. Watch it register, heartbeat, and start receiving jobs.

## Earning model

Revenue from each request is split **70/30** — providers receive **70%** in USDC
of the revenue from the requests their node serves; the remaining 30% funds the
network (split 50% buyback / 30% treasury / 20% operations). Actual earnings
depend on uptime, model demand, and how many other nodes are competing for jobs.

Your USDC earnings and your staked ORVX are tracked separately: withdraw earnings
anytime via `POST /v1/provider/withdraw`; recover your stake via
`POST /v1/staking/unstake`.

See [Tokenomics](./tokenomics.md) for how rewards and the broader token economy
fit together.

## Next steps

- [orvix-node/README.md](../orvix-node/README.md) — detailed node setup and
  configuration.
- [FAQ](./faq.md) — common provider questions.
