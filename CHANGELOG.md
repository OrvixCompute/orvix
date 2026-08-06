# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Node multi-engine architecture: `AbstractEngine` base with `ChatEngine`/`ImageEngine` families and a `model_id → engine_type` router (foundation for image generation)
- `FluxEngine` — Flux Schnell text-to-image via Diffusers (bfloat16, 1024×1024 / 4 steps defaults); heavy GPU deps imported lazily
- `OrvixImageEngine` — 4-step distilled text-to-image via Diffusers (fp16, guidance-free); no gated upstream access and a smaller on-disk footprint than Flux Schnell, so it is the node's default registered image engine (`FluxEngine` stays in the codebase, unregistered, for a future gated-access re-enable)
- Node advertises `engines` + `vram_gb` at registration (additive, backward-compatible; image is opt-in via `enable_image_engine`)
- `image` optional extra (diffusers/transformers/accelerate/…) and opt-in `scripts/download_flux.py` pre-download helper
- `ModelManager` — swaps chat/image engines through a single GPU's VRAM with a swap lock, drain-before-unload, idle unload (default 10 min), and thrash detection
- `ModelManager` concurrent mode (`max_resident` param, node config `concurrent_engines`): keeps chat + image both resident in VRAM instead of swapping, for deployments where the combined footprint fits (e.g. an AWQ-quantized chat model next to an image engine) — LRU eviction only kicks in past capacity, per-engine idle unload, `shutdown()` unloads everything resident
- Managed vLLM mode (`vllm_managed`): the node owns the vLLM server as a subprocess so `unload()` actually frees VRAM for the image engine (start on load, kill on unload)
- Node `/v1/status` endpoint: current engine, VRAM free/total, uptime, active jobs
- Config: `vllm_managed`, `idle_unload_minutes`
- **Image generation (orchestrator):** `POST /v1/images/generations` (OpenAI DALL-E-compatible) — dispatches to an image-capable node, fetches the PNG from the node's binary endpoint, saves it, returns URL/b64
- `GET /v1/models` catalog endpoint (chat + `flux-schnell`/`orvix-image-1` image models)
- Protocol messages `job.image.dispatch` / `job.image.complete` / `job.image.failed`; `RegisterMessage` gains optional `engines[]` + `vram_gb`
- Node binary endpoint `GET /v1/binary/image/<id>` (per-job `X-Node-Secret` token, stream-then-delete) + node image job handler
- Node manager reads node capabilities and routes image jobs only to image-capable nodes
- Migrations `010_image_jobs`, `011_node_capabilities`; config `IMAGE_JOB_TIMEOUT`, `IMAGE_STORAGE_DIR`, `PUBLIC_IMAGE_URL_BASE`; node config `image_tmp_dir`, `binary_public_url`
- **Quota system:** holder verification (`holder.py`, 15-min cached ORVX balance) + `quota_service`; chat free tier (2 lifetime for non-holders, then pay-or-402), image daily limits (5/day holders; 1/day grace for everyone when `ORVX_MINT_ADDRESS` unset; non-holders 403 when the mint is set; 429 over limit, resets 00:00 UTC). `GET /v1/account/quota`, quota response headers on chat/images. Migration `012_quotas`; config `ORVX_HOLDER_THRESHOLD`, `CHAT_LIFETIME_FREE_LIMIT`, `IMAGE_DAILY_LIMIT_HOLDER`, `IMAGE_DAILY_LIMIT_FALLBACK`, `HOLDER_CACHE_TTL_MINUTES`, `UPGRADE_URL`, `TOKENOMICS_URL`
- **Public network stats:** `GET /v1/network/stats` — the compute-side dashboard feed (node/GPU capacity, all-time + rolling-window request/token/image volume, avg latency, provider and model counts). Aggregation runs in one DB round trip via the `network_stats()` SQL function (migration `014_network_stats`, plus `created_at` indexes on `jobs`/`image_jobs`); mock jobs are excluded. The snapshot is cached (`NETWORK_STATS_CACHE_SECONDS`, default 30) since the endpoint is unauthenticated, while `nodes.online` is read live from the websocket registry on every call. Config `NETWORK_STATS_CACHE_SECONDS`, `NETWORK_STATS_WINDOW_HOURS`. Complements the existing token-side `GET /v1/staking/network-stats`
- `GET /v1/admin/feature-flags` now also reports the withdrawal economics (`min_withdraw_amount_usdc`, `auto_approve_max_usdc`, `max_withdrawals_per_day`). These are `.env`-only and read at request time, so there was previously no way to confirm from outside the server that a config edit had taken effect. The endpoint is also now documented in the API reference, where it had been missing entirely
- **Image storage lifecycle:** 24h auto-delete via `scripts/cleanup_images.py` + hourly systemd timer (`scripts/systemd/`), orphan-file + stale-holder sweeps; `MAX_IMAGE_STORAGE_MB` safety cap (503 before quota when full); `GET /v1/admin/storage/stats`; image quota refunded on generation failure; recent-images list on `/v1/account/quota`; node 10-min temp-file sweeper. Docs: operations/api-reference/ARCHITECTURE updated

### Changed
- Unified engine lifecycle to `load(model_id)` / `unload` / `is_loaded` across all engines (renamed from `initialize`/`is_ready`/`shutdown`)
- The executor no longer owns a single backend; it routes each job through the `ModelManager`, loading/swapping the right engine on demand
- Default image model swapped from `flux-schnell` to `orvix-image-1` (`ImageGenerationRequest` default, node's registered image engine); `flux-schnell` stays in the catalog and router for backward compatibility
- `ModelManager` no longer holds its lock during the actual `load()`/`unload()` I/O — state transitions are decided and committed under the lock, but the slow work runs with it released. In concurrent mode this was a real bug: a ~60s image cold-load blocked *every* other request (including one for an already-resident, untouched chat engine) because the single lock covering the whole load spanned the entire I/O

### Fixed
- Migration `016_jobs_provider_id` records the provider on each job. `jobs` stored who ran the request and which node served it, but the provider was only reachable via `jobs → nodes → provider_id` — so deleting a node destroyed the attribution for every job it had served, including the `provider_earning_usdc` already booked against them. Node deletion is routine, and after `015` it nulls `jobs.node_id` by design, which severed the last link. `image_jobs` has stored `provider_id` directly since migration `010`; this brings the older table in line rather than inventing a new pattern. Existing rows are back-filled where the node row still resolves. The column and its constraint are added in **separate** statements, because a combined `add column if not exists … references …` is precisely what left `jobs.node_id` without a foreign key (see `015`)
- Migration `015_jobs_node_fk` repairs the `jobs.node_id` foreign key, which has never existed in any database built from this migration set. `002_nodes.sql` wrote `alter table jobs add column if not exists node_id uuid references nodes(id) on delete set null`, but `001` had already created the column — so `add column if not exists` became a no-op and took the `REFERENCES` clause with it. Reproduced on a clean Postgres 16: after 001–014 there is no constraint on `jobs.node_id` and a job referencing a nonexistent node is accepted. The migration nulls references left dangling in the meantime, adds the constraint behind a `pg_constraint` guard so it stays re-runnable, and indexes the referencing column (Postgres indexes only the referenced side, and `ON DELETE SET NULL` has to find these rows on every node deletion)
- Node identity is now stable across reconnects. `register_node()` minted a fresh `uuid4()` on every connection, so a single machine left an `offline` ghost row in `nodes` each time it restarted — one RTX A4500 was briefly reported to the public `GET /v1/network/stats` as "2 nodes, 40 GB VRAM". The node now generates an id once, caches it in a `node-id` file beside its config (not under `~`, which is the ephemeral layer on a container host), and sends it in `RegisterMessage`; the orchestrator reuses that row. The field is optional, so older nodes keep the previous per-connection behaviour, and a claimed id is honoured only when unused or already owned by the same provider — otherwise a provider could take over another provider's row along with its job history. New node config field `node_id` pins it explicitly
- `POST /v1/images/generations` validated the requested size against one global list, ignoring the model's catalog `max_size`. Sizes the chosen model cannot serve were accepted and dispatched, so the failure surfaced on the node — after a job slot and quota had already been spent — instead of as a 400. Sizes are now checked per model, and the error lists only what that model actually offers. Two entries in the list (`1024x1792`, `1792x1024`) exceeded every model's maximum *and* the node protocol's own 1536-per-dimension cap, so they could never have succeeded for anyone
- The node executor limited chat and image jobs with a single shared semaphore, so `max_concurrent_jobs: 4` allowed four simultaneous diffusion passes. A generation needs several GB of transient VRAM on top of the resident weights — measured at 19.6 of 20.4 GiB for one 1024×1024 pass next to a resident chat engine — so two at once OOM a card that serves one comfortably. Image jobs now have their own limit (`max_concurrent_image_jobs`, default 1) and no longer occupy chat slots; chat and image still run concurrently

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
