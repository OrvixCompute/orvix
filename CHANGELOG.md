# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Node multi-engine architecture: `AbstractEngine` base with `ChatEngine`/`ImageEngine` families and a `model_id → engine_type` router (foundation for image generation)
- `FluxEngine` — Flux Schnell text-to-image via Diffusers (bfloat16, 1024×1024 / 4 steps defaults); heavy GPU deps imported lazily
- Node advertises `engines` + `vram_gb` at registration (additive, backward-compatible; image is opt-in via `enable_image_engine`)
- `image` optional extra (diffusers/transformers/accelerate/…) and opt-in `scripts/download_flux.py` pre-download helper
- `ModelManager` — swaps chat/image engines through a single GPU's VRAM with a swap lock, drain-before-unload, idle unload (default 10 min), and thrash detection
- Managed vLLM mode (`vllm_managed`): the node owns the vLLM server as a subprocess so `unload()` actually frees VRAM for the image engine (start on load, kill on unload)
- Node `/v1/status` endpoint: current engine, VRAM free/total, uptime, active jobs
- Config: `vllm_managed`, `idle_unload_minutes`
- **Image generation (orchestrator):** `POST /v1/images/generations` (OpenAI DALL-E-compatible) — dispatches to an image-capable node, fetches the PNG from the node's binary endpoint, saves it, returns URL/b64
- `GET /v1/models` catalog endpoint (chat + `flux-schnell` image model)
- Protocol messages `job.image.dispatch` / `job.image.complete` / `job.image.failed`; `RegisterMessage` gains optional `engines[]` + `vram_gb`
- Node binary endpoint `GET /v1/binary/image/<id>` (per-job `X-Node-Secret` token, stream-then-delete) + node image job handler
- Node manager reads node capabilities and routes image jobs only to image-capable nodes
- Migrations `010_image_jobs`, `011_node_capabilities`; config `IMAGE_JOB_TIMEOUT`, `IMAGE_STORAGE_DIR`, `PUBLIC_IMAGE_URL_BASE`; node config `image_tmp_dir`, `binary_public_url`

### Changed
- Unified engine lifecycle to `load(model_id)` / `unload` / `is_loaded` across all engines (renamed from `initialize`/`is_ready`/`shutdown`)
- The executor no longer owns a single backend; it routes each job through the `ModelManager`, loading/swapping the right engine on demand

## [0.2.0] — 2026-06-26 — Whitepaper Alignment

### Added
- Provider staking: 25,000 ORVX minimum required to register as a compute provider
- Stake-based tier system (Bronze/Silver/Gold/Diamond) replacing hold-based
- Buyback engine: manual admin tooling (CLI + endpoint) to swap USDC revenue → ORVX via Jupiter
- Burn mechanism: monthly burn of bought-back ORVX to the incinerator address
- Revenue split: 70% provider, 30% platform (of which 50% buyback, 30% treasury, 20% ops), recorded per job
- Snapshot.org integration for off-chain governance (`/v1/governance/snapshot-url` + docs)
- New endpoints: `/v1/staking/*`, `/v1/account/tier`, `/v1/admin/buyback/*`, `/v1/admin/burn/*`, `/v1/governance/*`
- Public transparency: buyback-history, burn-history, and network-stats endpoints
- Admin auth via `X-Admin-Key` (ADMIN_API_KEY)
- Database migrations 006, 007, 008 for staking, buyback/burn accounting, and stake-based tiers
- Monthly burn-reminder systemd timer; buyback/burn CLIs under `scripts/`
- Docs: governance set, burn procedure, scripts README

### Changed
- Tier is now derived from `staked_orvx` (kept in sync by a DB trigger), not wallet balance
- Provider registration enforces the minimum stake when `REQUIRE_STAKE_FOR_PROVIDER` is enabled (default off during alpha)
- Inference billing applies the stake-based tier discount
- `RequestValidationError` responses are now JSON-safe when error context contains Decimals

## [0.1.0] — Unreleased — Backend MVP

### Added
- Orchestrator: wallet-based authentication via Phantom signature → JWT
- Orchestrator: API key management (create, list, rotate, delete) with sha256 hashing
- Orchestrator: OpenAI-compatible inference endpoint with a mock backend
- Orchestrator: tier-aware node selection (Bronze/Silver/Gold/Diamond)
- Orchestrator: node WebSocket endpoint with registration, heartbeat, and job dispatch
- Orchestrator: provider endpoints for node management
- Node: CLI (start, status, logs, config, gpu, test-inference)
- Node: GPU detection via `pynvml` with a stub mode for GPU-less development
- Node: WebSocket client with auth, heartbeat, and exponential-backoff reconnect
- Node: job executor with swappable inference backends and concurrency limiting
- Node: mock inference backend
- Node: vLLM backend skeleton (requires a GPU)
- Database: schema for users, api_keys, nodes, and jobs
- Tests: 61 unit tests across orchestrator (43) and node (18)
- Integration: verified cross-process end-to-end flow (node ↔ orchestrator via WebSocket)
- Docs: README, ARCHITECTURE, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, LICENSE

### Known Limitations
- Real vLLM integration pending (requires a GPU)
- No deployed production environment yet
- Frontend not yet implemented
- Challenge-nonce store and rate limiter are in-memory (single-process) — shared store planned

[Unreleased]: https://github.com/OrvixCompute/orvix/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/OrvixCompute/orvix/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/OrvixCompute/orvix/releases/tag/v0.1.0
