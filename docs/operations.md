# Operations

Operational runbook notes for running Orvix in production.

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
