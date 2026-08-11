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
curl https://orvix.network/v1/chat/completions \
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

- [Using an API key](./api-keys.md) — how to create one, which models are
  actually being served, quotas, and the errors worth handling.
- Read [orchestrator/README.md](../orchestrator/README.md) for backend setup and
  how authentication / API keys work.
- See the [API Reference](./api-reference.md) for every endpoint (including
  `/v1/staking/*`, `/v1/account/tier`, and `/v1/videos/generations`).

## For providers (run a node)

A provider installs a lightweight agent that connects to the orchestrator over
WebSocket and executes inference jobs on your GPU.

**Staking is not required during the alpha.** The whitepaper sets a 2,000,000 ORVX
minimum to register as a provider, but the gate is switched off
(`REQUIRE_STAKE_FOR_PROVIDER=false`), so registration succeeds with nothing
staked. Expect it to be enforced before the public testnet. See the
[Provider Guide](./provider-guide.md#provider-requirements).

```bash
curl -sSL https://raw.githubusercontent.com/OrvixCompute/orvix/main/orvix-node/install.sh | bash
orvix-node join     # paste the provider_id + node_secret you registered with
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
