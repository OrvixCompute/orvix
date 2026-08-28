# Operations

Operational runbook notes for running Orvix in production.

## Observability

### Logs

The orchestrator logs to **stdout only** (JSON lines in prod, human-readable in
dev) via loguru. In production, pipe stdout into your log aggregator
(journald → Loki/Datadog, etc).

Every HTTP request gets a `request_id` UUID:

- returned to the client as the **`X-Request-ID`** response header,
- included in the error envelope (`error.request_id`) on 4xx/5xx responses,
- bound as a loguru contextual field for the whole request, so **every log
  line emitted while serving it carries `request_id`** in the JSON `extra`
  object — including service-layer lines that have no request object of their
  own.

Key structured log lines:

- `request ...` / `request_failed ...` — one line per HTTP request with
  `method`, `path`, `status`, `duration_ms`, `request_id`.
- `intel_scan scan_type=... target=... cache_hit=... duration_ms=...` — one
  line per token-intel scan (token, wallet, accumulation, holders,
  early_buyers, social, intelligence) showing cache efficiency and latency.
  `scan_type`/`target` are also attached to the Sentry event when the scan
  fails (see below).
- `monitor_cycle monitors_evaluated=... alerts_fired=... webhooks_dispatched=...`
  — one line per monitor-worker cycle that evaluated at least one monitor.
- `intel_error scan_type=... target=...` — fail-soft intel failures, logged
  with the exception and reported to Sentry as a warning tagged with the scan
  context.

### Sentry

Error tracking is opt-in: set `SENTRY_DSN` (get it from the Sentry project
settings) and optionally `SENTRY_TRACES_SAMPLE_RATE` (default 0.1). When the
DSN is empty nothing is initialized and no events are sent.

- **Env config:** `.env.example` documents both variables; `main.py` calls
  `sentry_sdk.init()` with the FastAPI + Logging integrations, the
  `ENVIRONMENT` as the environment, and the orchestrator version as the
  release. `send_default_pii=False` — wallet addresses and request bodies are
  never shipped.
- **Request context:** unhandled 5xx errors are captured with `request_id` and
  `path` tags so an error in the Sentry dashboard can be correlated back to a
  log line.
- **Intel context:** fail-soft token-intel failures are captured as
  **warning-level** events tagged with `scan_type` and `target`, so repeated
  failures for one mint/wallet are filterable — the signal that a data source
  is broken for a particular token, not the network at large.

**Verify a deployment** by triggering a known error (e.g. a bad wallet address
on an endpoint that resolves it through the intel layer) and confirming the
event shows up in the Sentry dashboard with the `scan_type`/`target` tags.

## Code Synchronization Discipline

**The repository is the single source of truth for all code.** Anything running
in production must be reproducible from a clean `git clone`.

### The hazard

GPU compute nodes typically run on **ephemeral container storage**. Code written
or patched directly on a node is **lost** when:

- the container is restarted (ephemeral storage is wiped),
- the node is terminated, or
- a node is redeployed from git (overwrites local edits).

This actually happened: a working **vLLM HTTP-proxy backend** was implemented
directly on a deployed GPU node during an end-to-end test, while the repo still
carried a `NotImplementedError` skeleton
(`orvix-node/orvix_node/inference/vllm.py`). The implementation survived only
because it had also been copied into a local working tree; it was later ported
back to the repo on branch `feat/sync-vllm-backend`. Had the node been recycled
first, the work would have been gone.

### The rule

> Any code written directly on a deployed GPU node **MUST** be ported back to the
> repo **before** the next deploy or container restart.

### Checklist after any on-node work

1. On the node checkout: run `git status` and `git diff` to see every change.
2. Commit to a branch and `git push`, **or** copy the changed files into your
   local repo and commit there.
3. Re-run `git status` on the node and confirm it is **clean** — nothing
   uncommitted, nothing untracked that matters.
4. Only then stop, restart, or redeploy the node.

### Related deploy notes

- The production orchestrator at `/opt/orvix` is currently a **file copy**, not a
  git checkout — deploys rsync `orchestrator/` from a fresh clone of `main`
  (preserving `.env` and `.venv`). Treat `main` as the source of truth and keep
  the VPS in sync with it.
- Prefer making the VPS checkout a real `git` clone so `git status` there can
  catch drift the same way.

## Image storage & cleanup

Generated images are written to `IMAGE_STORAGE_DIR` (default `/var/orvix/images`)
and served by nginx at `PUBLIC_IMAGE_URL_BASE`. Each image is **auto-deleted after
24 hours** (tracked via `image_jobs.expires_at`).

### Install the cleanup timer (one-time, manual)

```bash
sudo cp orchestrator/scripts/systemd/orvix-image-cleanup.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now orvix-image-cleanup.timer
systemctl list-timers | grep orvix          # confirm it's scheduled
```

The timer runs hourly (`OnUnitActiveSec=1h`, `OnBootSec=5min`). The service is a
oneshot that runs `scripts/cleanup_images.py` from `/opt/orvix/orchestrator`
(pydantic loads `.env` from that working directory — no `EnvironmentFile` needed).

### What cleanup does

1. Deletes `image_jobs` rows whose `expires_at` has passed, plus their files.
2. Sweeps orphan files (on disk, no DB row) older than 25h (1h grace).
3. Prunes `holder_status` rows not refreshed in 7 days.

Exit code is non-zero if any deletion failed, so `systemctl status
orvix-image-cleanup` / `journalctl -u orvix-image-cleanup` surfaces problems.

### Manual run

```bash
cd /opt/orvix/orchestrator && .venv/bin/python scripts/cleanup_images.py
```

### Monitoring

- Logs: `journalctl -u orvix-image-cleanup.service -n 50`
- Disk usage: `GET /v1/admin/storage/stats` (X-Admin-Key) →
  `{total_files, total_size_mb, max_size_mb, oldest_file_age_hours}`.

### Storage safety cap

`MAX_IMAGE_STORAGE_MB` (default 5000) bounds `IMAGE_STORAGE_DIR`. When exceeded,
`POST /v1/images/generations` returns `503 storage_full` (before consuming quota)
until cleanup frees space. The size is cached for 60s to keep the check cheap.

### Node temp files

Provider nodes write images to `/tmp/node-images` and delete them on fetch. A
background sweeper (every 10 min) removes any file older than 1h that was never
fetched, so a crashed transfer doesn't leak disk.
