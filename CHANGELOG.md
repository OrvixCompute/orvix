# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- (entries here as work progresses)

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

[Unreleased]: https://github.com/OrvixCompute/orvix/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/OrvixCompute/orvix/releases/tag/v0.1.0
