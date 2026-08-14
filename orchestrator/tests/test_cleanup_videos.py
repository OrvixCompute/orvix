"""Tests for scripts/cleanup_videos.py (expired rows + files, orphan sweep)."""

import os
from datetime import datetime, timedelta, timezone

from scripts.cleanup_videos import run_cleanup
from tests.fakes import FakeSupabase


def _add_video(db, **fields):
    row = {
        "id": fields.get("id", "vid-1"),
        "video_url": f"https://orvix.network/videos/{fields.get('id', 'vid-1')}.mp4",
        "expires_at": fields.get("expires_at"),
    }
    return db._table("video_jobs").insert_row(row)


def test_deletes_expired_rows_and_files(tmp_path):
    db = FakeSupabase()
    now = datetime.now(timezone.utc)
    expired = tmp_path / "old.mp4"
    expired.write_bytes(b"OLD")
    fresh = tmp_path / "new.mp4"
    fresh.write_bytes(b"NEW")

    _add_video(db, id="old", expires_at=(now - timedelta(hours=2)).isoformat())
    _add_video(db, id="new", expires_at=(now + timedelta(hours=2)).isoformat())
    # The two files above correspond to the two rows.
    db._table("video_jobs").rows[0]["video_url"] = f"file:///{expired}"
    db._table("video_jobs").rows[1]["video_url"] = f"file:///{fresh}"

    result = run_cleanup(db, str(tmp_path), now=now)

    assert result["rows_deleted"] == 1
    assert result["files_deleted"] == 1
    assert result["failures"] == 0
    assert not expired.exists()  # expired file removed
    assert fresh.exists()  # fresh file kept
    remaining = [r["id"] for r in db._table("video_jobs").rows]
    assert remaining == ["new"]


def test_sweeps_orphan_files_after_grace(tmp_path):
    db = FakeSupabase()
    now = datetime.now(timezone.utc)
    # No rows at all -> every file on disk is an orphan.
    orphan_old = tmp_path / "orphan-old.mp4"
    orphan_old.write_bytes(b"X")
    # mtime older than the 25h grace window.
    old_ts = now.timestamp() - 26 * 3600
    os.utime(orphan_old, (old_ts, old_ts))
    orphan_new = tmp_path / "orphan-new.mp4"
    orphan_new.write_bytes(b"Y")

    result = run_cleanup(db, str(tmp_path), now=now)

    assert result["orphans_deleted"] == 1
    assert not orphan_old.exists()
    assert orphan_new.exists()  # too fresh for the grace window


def test_keeps_files_referenced_by_rows(tmp_path):
    db = FakeSupabase()
    now = datetime.now(timezone.utc)
    _add_video(db, id="live", expires_at=(now + timedelta(hours=2)).isoformat())
    kept = tmp_path / "live.mp4"
    kept.write_bytes(b"KEEP")

    result = run_cleanup(db, str(tmp_path), now=now)

    assert result["rows_deleted"] == 0
    assert result["orphans_deleted"] == 0
    assert kept.exists()
