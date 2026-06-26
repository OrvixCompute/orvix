# Getting Started

> 📝 This guide is a work in progress. For now, the package READMEs are the
> source of truth — this page just points you to the right one.

Orvix has two kinds of users:

- **Developers** who want to *use* the network to run AI inference through an
  OpenAI-compatible API.
- **Providers** who want to *run a node*, contribute their GPU, and earn for
  serving requests.

Pick the path that matches you.

## For developers (use the API)

You talk to Orvix through an OpenAI-compatible endpoint, so most existing
OpenAI client libraries work by just changing the base URL and API key.

```bash
curl https://api.orvix.xyz/v1/chat/completions \
  -H "Authorization: Bearer orvx_sk_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen-2.5-7b",
    "messages": [{"role": "user", "content": "Hello, Orvix!"}]
  }'
```

Pricing is per token, with a **stake-based discount** (up to 25% off) — stake ORVX
to lower your per-token cost. See [Tokenomics](./tokenomics.md#premium-access-tiers)
and `GET /v1/account/tier`.

Next steps:

- Read [orchestrator/README.md](../orchestrator/README.md) for backend setup and
  how authentication / API keys work.
- See the [API Reference](./api-reference.md) for every endpoint (including
  `/v1/staking/*` and `/v1/account/tier`).

## For providers (run a node)

A provider installs a lightweight agent that connects to the orchestrator over
WebSocket and executes inference jobs on your GPU. **Heads-up:** becoming a
provider requires staking **25,000 ORVX** in addition to eligible hardware — see
the [Provider Guide](./provider-guide.md#provider-requirements).

```bash
curl -fsSL https://get.orvix.xyz | sh   # placeholder install script
orvix-node start
```

Next steps:

- Read [orvix-node/README.md](../orvix-node/README.md) for detailed node setup,
  including the GPU-less stub mode for development.
- See the [Provider Guide](./provider-guide.md) for hardware requirements and the
  earning model.

## Building from source

If you want to hack on Orvix itself, both packages have their own setup
instructions, and [CONTRIBUTING.md](../CONTRIBUTING.md) covers the workflow.
