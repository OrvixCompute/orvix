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

- 🌐 Website — https://orvix.xyz *(placeholder)*
- 📚 Documentation — https://docs.orvix.xyz *(placeholder)*
- 🧩 API reference — [orchestrator/README.md](orchestrator/README.md)
- 📄 Whitepaper — *coming soon*
- 💬 [Discord](https://discord.gg/orvix) · [Twitter](https://twitter.com/OrvixCompute) · [Telegram](https://t.me/orvix)

## Architecture overview

```
┌─────────────┐      OpenAI-compatible       ┌──────────────┐      WebSocket       ┌──────────────┐
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
curl https://api.orvix.xyz/v1/chat/completions \
  -H "Authorization: Bearer orvx_sk_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen-2.5-7b",
    "messages": [{"role": "user", "content": "Hello, Orvix!"}]
  }'
```

**For providers (run a node):**

```bash
curl -fsSL https://get.orvix.xyz | sh   # placeholder install script
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

**Active development — backend MVP complete, public testnet incoming.**

Both packages are built and unit-tested, with a cross-process end-to-end flow verified
(node ↔ orchestrator over WebSocket). Real GPU inference (vLLM) and a public deployment are
the next milestones. See [CHANGELOG.md](CHANGELOG.md) for details.

## 📖 Documentation

- [orchestrator/README.md](orchestrator/README.md) — backend setup and API
- [orvix-node/README.md](orvix-node/README.md) — running a node
- [ARCHITECTURE.md](ARCHITECTURE.md) — system design deep-dive
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
