<!-- Placeholder logo at .github/assets/logo.svg — swap with the final design when ready -->
<p align="center">
  <img src=".github/assets/logo.svg" alt="Orvix" width="400">
</p>

# Orvix

> Decentralized GPU inference network on Solana. Powering intelligence at scale.

[![Tests](https://github.com/OrvixCompute/orvix/actions/workflows/test.yml/badge.svg)](https://github.com/OrvixCompute/orvix/actions/workflows/test.yml)
[![Protocol Sync](https://github.com/OrvixCompute/orvix/actions/workflows/protocol-sync.yml/badge.svg)](https://github.com/OrvixCompute/orvix/actions/workflows/protocol-sync.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Twitter](https://img.shields.io/badge/Twitter-%40OrvixCompute-1DA1F2.svg)](https://twitter.com/OrvixCompute)
[![Discord](https://img.shields.io/badge/Discord-join-5865F2.svg)](https://discord.gg/orvix)

Orvix is a decentralized network that connects AI developers with a community of GPU
providers. Developers reach distributed compute through a single OpenAI-compatible API;
providers turn idle GPUs into useful capacity by running a lightweight node. The result is
open, community-owned inference with no vendor lock-in.

> ⚠️ **Early development (alpha).** The backend MVP and node software are built and tested,
> but the project is not production-ready. Expect breaking changes.

## ⚡ Quick links

- 🌐 Website — https://orvix.network *(placeholder)*
- 📚 Documentation — https://docs.orvix.network *(placeholder)*
- 🧩 API reference — [orchestrator/README.md](orchestrator/README.md)
- 📄 Whitepaper — *coming soon*
- 💬 [Discord](https://discord.gg/orvix) · [Twitter](https://twitter.com/OrvixCompute) · [Telegram](https://t.me/orvix)

## Architecture overview

```
┌─────────────┐      OpenAI-compatible       ┌──────────────┐      WebSocket      ┌──────────────┐
│  Developer  │ ───────────  HTTPS  ───────▶ │ Orchestrator │ ─────────────────▶  │   Node(s)    │
│  (API call) │ ◀──────────  response  ───── │   (FastAPI)  │ ◀─────────────────  │  (GPU agent) │
└─────────────┘                              └──────────────┘                     └──────────────┘
```

- **Developer** calls the OpenAI-compatible endpoint with an API key.
- **Orchestrator** authenticates the request and routes it to a suitable node.
- **Node(s)** run on provider machines, execute the inference job, and stream results back.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the deep-dive.

## 📦 Monorepo structure

```
orvix/
├── orchestrator/    # FastAPI backend — auth, API keys, routing, node management
├── orvix-node/      # Python agent — runs on GPU provider machines
├── .github/         # CI workflows, issue/PR templates
├── docs/            # Additional documentation
└── README.md        # You are here
```

- **orchestrator/** — the API gateway that authenticates developers and dispatches jobs to nodes.
- **orvix-node/** — the agent a provider installs to join the network and serve inference.
- **.github/** — continuous integration and contributor templates.
- **docs/** — supplementary guides and references.

## 🚀 Quick start

**For developers (use the API):** create an API key, then call the OpenAI-compatible endpoint.

```bash
curl https://api.orvix.network/v1/chat/completions \
  -H "Authorization: Bearer orvx_sk_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen-2.5-7b",
    "messages": [{"role": "user", "content": "Hello, Orvix!"}]
  }'
```

**For providers (run a node):**

```bash
curl -fsSL https://get.orvix.network | sh   # placeholder install script
orvix-node start
```

**For contributors (build from source):** see [orchestrator/README.md](orchestrator/README.md) and
[orvix-node/README.md](orvix-node/README.md), plus [CONTRIBUTING.md](CONTRIBUTING.md).

## 🛠️ Tech stack

- **Backend:** Python 3.11+, FastAPI, Supabase (PostgreSQL), Solana via `solders` (wallet auth)
- **Transport:** WebSocket between orchestrator and nodes
- **Inference:** vLLM (planned) — targeting Llama 3, Mistral, and Qwen families
- **Node:** asyncio, `websockets`, GPU detection with a stub mode for GPU-less development

## 📍 Project status

**Active development — backend MVP + tokenomics complete, public testnet incoming.**

Both packages are built and unit-tested, with a cross-process end-to-end flow verified
(node ↔ orchestrator over WebSocket). The ORVX utility model is implemented: provider
staking (25k ORVX minimum), stake-based pricing tiers, a 70/30 revenue split feeding a
50/30/20 buyback/treasury/operations flow, manual buyback (Jupiter) and monthly burn
tooling, and Snapshot-based governance. On-chain buyback/burn execution is stub-gated
pending devnet testing. Real GPU inference (vLLM) and a public deployment are the next
milestones. See [CHANGELOG.md](CHANGELOG.md) and [docs/tokenomics.md](docs/tokenomics.md).

## ⚠️ Alpha state disclosures

**This is alpha software. Do not use it for production workloads or with funds you
cannot afford to lose.** We would rather be upfront about what is *not* yet real than
have early users discover it the hard way. As of this release:

- **On-chain money movement is stubbed.** Provider **payouts**, ORVX **buyback**
  (Jupiter swaps), and monthly **burn** all run behind stub flags (`PAYOUT_STUB`,
  `BUYBACK_STUB`, `BURN_STUB`, default `true`). The accounting and workflows are
  implemented and tested, but no real SPL transfers are executed yet — they emit
  simulated transaction signatures. Real execution lands after devnet testing.
- **The system is custodial, not trustless.** Staking, payouts, and treasury flows
  are settled off-chain by an operator-controlled treasury wallet against the database.
  **There is no on-chain program (no Solana/Anchor smart contract) yet**, so there are
  no on-chain guarantees, escrow, or automated enforcement.
- **No on-chain slashing or output verification.** The network does not yet
  cryptographically verify that a node returned honest inference results, and there is
  no slashing/dispute mechanism. Provider trust is currently operational, not enforced.
- **Staking is disabled for alpha** (`REQUIRE_STAKE_FOR_PROVIDER=false`). It activates
  ahead of the public testnet.
- **ORVX mint is not yet wired in-app.** The on-chain mint address is added when the
  payout implementation lands.
- **Single-process scale.** Auth challenge nonces and API rate limits are held
  in-memory, so the orchestrator runs as a single worker for now. Multi-worker /
  horizontal scaling (Redis-backed state) comes when real traffic warrants it.
- **Endpoints and links are placeholders.** `orvix.network`, `docs.orvix.network`, and the
  `get.orvix.network` install script are not live yet.

Expect breaking changes. Track progress in [CHANGELOG.md](CHANGELOG.md).

## 📖 Documentation

- [orchestrator/README.md](orchestrator/README.md) — backend setup and API
- [orvix-node/README.md](orvix-node/README.md) — running a node
- [ARCHITECTURE.md](ARCHITECTURE.md) — system design deep-dive
- [docs/tokenomics.md](docs/tokenomics.md) — ORVX utility, tiers, buyback & burn
- [docs/governance/README.md](docs/governance/README.md) — Snapshot governance
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to contribute
- [SECURITY.md](SECURITY.md) — reporting vulnerabilities

## 🤝 Contributing

Contributions welcome — code, docs, bug reports, and ideas. Start with
[CONTRIBUTING.md](CONTRIBUTING.md) and the
[good first issues](https://github.com/OrvixCompute/orvix/labels/good%20first%20issue).

## 🔒 Security

Found a security issue? Please see [SECURITY.md](SECURITY.md) — **do not open a public issue.**

## 📜 License

Licensed under the [Apache License 2.0](LICENSE).

## 🌐 Community

- Twitter — [@OrvixCompute](https://twitter.com/OrvixCompute)
- Discord — [discord.gg/orvix](https://discord.gg/orvix)
- Telegram — [t.me/orvix](https://t.me/orvix)
- Newsletter — *signup coming soon*
