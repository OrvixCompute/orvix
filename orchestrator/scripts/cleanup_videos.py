#!/usr/bin/env python3
"""Video storage cleanup — delete expired video jobs/rows, sweep orphans.

Runs hourly via systemd (scripts/systemd/orvix-video-cleanup.timer) or manually:

    cd /opt/orvix/orchestrator && .venv/bin/python scripts/cleanup_videos.py

Steps:
  1. Delete video_jobs whose expires_at < now, and their files on disk.
  2. Sweep orphan files (on disk, no matching row) older than the grace period.

Mirrors cleanup_images.py but without the holder_status prune — video has no
holder cache to maintain. Exits non-zero if any deletion failed, so the systemd
unit surfaces an alert.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone


def _filename_from_url(url: str) -> str:
    return url.rsplit("/", 1)[-1] if url else ""


def run_cleanup(
    db,
    storage_dir: str,
    now: datetime | None = None,
    orphan_grace_hours: float = 25.0,
) -> dict:
    """Perform the cleanup against `db` + `storage_dir`. Returns count summary."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    result = {
        "rows_deleted": 0,
        "files_deleted": 0,
        "orphans_deleted": 0,
        "failures": 0,
    }

    # 1. Expired jobs + their files.
    expired = (
        db.table("video_jobs")
        .select("id,video_url,expires_at")
        .lt("expires_at", now_iso)
        .execute()
    )
    for row in expired.data or []:
        fname = _filename_from_url(row.get("video_url", ""))
        fpath = os.path.join(storage_dir, fname) if fname else None
        if fpath and os.path.exists(fpath):
            try:
                os.remove(fpath)
                result["files_deleted"] += 1
            except OSError as exc:
                result["failures"] += 1
                print(f"WARN: failed to delete file {fpath}: {exc}", file=sys.stderr)
        try:
            db.table("video_jobs").delete().eq("id", row["id"]).execute()
            result["rows_deleted"] += 1
        except Exception as exc:  # noqa: BLE001
            result["failures"] += 1
            print(f"WARN: failed to delete row {row.get('id')}: {exc}", file=sys.stderr)

    # 2. Orphan files: on disk, not referenced by any remaining row, past grace.
    remaining = db.table("video_jobs").select("video_url").execute()
    known = {_filename_from_url(r.get("video_url", "")) for r in (remaining.data or [])}
    cutoff = now.timestamp() - orphan_grace_hours * 3600
    if os.path.isdir(storage_dir):
        for name in os.listdir(storage_dir):
            if name in known:
                continue
            fpath = os.path.join(storage_dir, name)
            try:
                if not os.path.isfile(fpath):
                    continue
                if os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    result["orphans_deleted"] += 1
            except OSError as exc:
                result["failures"] += 1
                print(f"WARN: failed to delete orphan {fpath}: {exc}", file=sys.stderr)

    return result


def main() -> int:
    # Imported here so the module stays importable in tests without app env.
    from app.config import settings
    from app.database import get_supabase

    result = run_cleanup(get_supabase(), settings.VIDEO_STORAGE_DIR)
    print(
        "cleanup: "
        f"files_deleted={result['files_deleted']} "
        f"rows_deleted={result['rows_deleted']} "
        f"orphans_deleted={result['orphans_deleted']} "
        f"failures={result['failures']}"
    )
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
