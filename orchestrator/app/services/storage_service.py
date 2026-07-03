"""Image storage accounting: directory size (cached) + stats.

`current_size_mb()` is cached for 60s so the per-request storage cap check on
POST /v1/images/generations doesn't os.walk the directory on every call.
`stats()` always computes fresh (used by the admin monitoring endpoint).
"""

from __future__ import annotations

import os
import time

from app.config import settings

_CACHE_TTL_S = 60.0
_cache: dict = {"mb": None, "ts": 0.0}


def _iter_files(path: str):
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            try:
                yield fp, os.stat(fp)
            except OSError:
                continue


def compute_size_mb(path: str | None = None) -> float:
    path = path or settings.IMAGE_STORAGE_DIR
    total = sum(st.st_size for _fp, st in _iter_files(path))
    return total / (1024 * 1024)


def current_size_mb() -> float:
    """Cached directory size in MB (recomputed at most once per 60s)."""
    now = time.monotonic()
    if _cache["mb"] is None or now - _cache["ts"] > _CACHE_TTL_S:
        _cache["mb"] = compute_size_mb()
        _cache["ts"] = now
    return _cache["mb"]


def stats(path: str | None = None) -> dict:
    path = path or settings.IMAGE_STORAGE_DIR
    total_bytes = 0
    count = 0
    oldest_mtime: float | None = None
    for _fp, st in _iter_files(path):
        count += 1
        total_bytes += st.st_size
        if oldest_mtime is None or st.st_mtime < oldest_mtime:
            oldest_mtime = st.st_mtime
    oldest_age_hours = (
        round((time.time() - oldest_mtime) / 3600, 2) if oldest_mtime is not None else None
    )
    return {
        "image_dir": path,
        "total_files": count,
        "total_size_mb": round(total_bytes / (1024 * 1024), 2),
        "max_size_mb": settings.MAX_IMAGE_STORAGE_MB,
        "oldest_file_age_hours": oldest_age_hours,
    }
