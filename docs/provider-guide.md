# Provider Guide

> 📝 Provider onboarding is in alpha. We're actively refining the experience,
> so steps and requirements may change.

## What is a provider?

A provider is someone who runs a GPU node on the Orvix network. Your machine
connects to the orchestrator, receives inference jobs, runs them on your GPU,
and streams the results back. In return, you earn a share of the revenue from
the requests you serve.

## Hardware requirements

- **GPU:** NVIDIA GPU with **8GB+ VRAM** minimum (16GB+ recommended, e.g.
  RTX 3090 / 4090, A4000 or better).
- **Drivers:** recent NVIDIA driver + CUDA 11 or newer.
- **OS:** Linux preferred.
- **Network:** stable connection with reasonable upload bandwidth.

> No GPU yet? The node ships with a **stub mode** (`ORVIX_NODE_STUB_GPU=true`)
> so you can develop and test the full flow without hardware.

## High-level steps

1. Create an account and register as a provider (`POST /v1/provider/register`),
   which returns your node secret.
2. Install the node agent (see [orvix-node/README.md](../orvix-node/README.md)).
3. Configure the agent with your provider ID and node secret.
4. Start the node: `orvix-node start`.
5. Watch it register, heartbeat, and start receiving jobs.

## Earning model

Revenue from each request is split **70/30** — providers receive **70%** of the
revenue from the requests their node serves; the remaining 30% funds the
network. Actual earnings depend on uptime, model demand, and how many other
nodes are competing for jobs.

See [Tokenomics](./tokenomics.md) for how rewards and the broader token economy
fit together.

## Next steps

- [orvix-node/README.md](../orvix-node/README.md) — detailed node setup and
  configuration.
- [FAQ](./faq.md) — common provider questions.
