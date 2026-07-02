"""End-to-end: the cleanup script deletes expired rows/files, orphans, stale cache."""

import importlib.util
import os
import pathlib
from datetime import datetime, timedelta, timezone

from tests.fakes import FakeSupabase

# Load scripts/cleanup_images.py by path (it isn't an importable package).
_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cleanup_images.py"
_spec = importlib.util.spec_from_file_location("cleanup_images", _SCRIPT)
cleanup_images = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cleanup_images)
run_cleanup = cleanup_images.run_cleanup


def _write(path, mtime_ts=None):
    path.write_bytes(b"PNGDATA")
    if mtime_ts is not None:
        os.utime(path, (mtime_ts, mtime_ts))


def test_cleanup_deletes_expired_orphans_and_stale_holders(tmp_path):
    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    db = FakeSupabase()

    # Expired job + file, and a live job + file.
    _write(tmp_path / "expired.png")
    _write(tmp_path / "live.png")
    db._table("image_jobs").insert_row(
        {"image_url": "https://x/images/expired.png", "expires_at": (now - timedelta(hours=1)).isoformat()}
    )
    db._table("image_jobs").insert_row(
        {"image_url": "https://x/images/live.png", "expires_at": (now + timedelta(hours=10)).isoformat()}
    )

    # Orphan files: one past the 25h grace (deleted), one fresh (kept).
    _write(tmp_path / "orphan_old.png", mtime_ts=(now - timedelta(hours=30)).timestamp())
    _write(tmp_path / "orphan_new.png", mtime_ts=(now - timedelta(hours=1)).timestamp())

    # Holder cache: one stale (>7d, pruned), one fresh (kept).
    db._table("holder_status").insert_row(
        {"wallet_address": "wstale", "last_checked_at": (now - timedelta(days=8)).isoformat()}
    )
    db._table("holder_status").insert_row(
        {"wallet_address": "wfresh", "last_checked_at": (now - timedelta(days=1)).isoformat()}
    )

    result = run_cleanup(db, str(tmp_path), now=now)

    assert result["failures"] == 0
    assert result["rows_deleted"] == 1
    assert result["files_deleted"] == 1
    assert result["orphans_deleted"] == 1
    assert result["holder_status_pruned"] == 1

    # Files: expired + old orphan gone; live + fresh orphan kept.
    assert not (tmp_path / "expired.png").exists()
    assert not (tmp_path / "orphan_old.png").exists()
    assert (tmp_path / "live.png").exists()
    assert (tmp_path / "orphan_new.png").exists()

    # Rows: only the live job + fresh holder remain.
    assert [r["image_url"] for r in db._table("image_jobs").rows] == ["https://x/images/live.png"]
    assert [r["wallet_address"] for r in db._table("holder_status").rows] == ["wfresh"]
