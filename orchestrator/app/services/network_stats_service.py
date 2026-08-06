"""Public network stats — the feed behind GET /v1/network/stats.

The heavy part (counting jobs, tokens, images, nodes) runs in the database via
the ``network_stats`` SQL function from migration 014, so it costs one round
trip instead of pulling every jobs row into the process.

That snapshot is cached for ``NETWORK_STATS_CACHE_SECONDS`` because the endpoint
is public and unauthenticated — without it every dashboard page load would hit
the database. The live node count is *not* cached: it comes from the in-memory
websocket registry and is free to read, so it stays accurate to the second.
"""

import time
from datetime import datetime, timezone

from supabase import Client

from app.config import settings
from app.models.inference import MODEL_CATALOG
from app.services.node_manager import node_manager

# (fetched_at_monotonic, snapshot dict). Module-level so it is shared across
# requests. A refresh stampede is harmless — worst case two identical queries.
_cache: tuple[float, dict] | None = None


def _model_counts() -> dict:
    return {
        "chat": sum(1 for m in MODEL_CATALOG if m["type"] == "chat"),
        "image": sum(1 for m in MODEL_CATALOG if m["type"] == "image"),
    }


def _fetch(db: Client) -> dict:
    """Run the aggregation and normalize it into the response shape."""
    raw = db.rpc(
        "network_stats", {"p_window_hours": settings.NETWORK_STATS_WINDOW_HOURS}
    ).execute().data or {}

    nodes = raw.get("nodes") or {}
    chat = raw.get("chat") or {}
    images = raw.get("images") or {}
    providers = raw.get("providers") or {}
    latency = chat.get("avg_latency_ms")

    return {
        "window_hours": raw.get("window_hours", settings.NETWORK_STATS_WINDOW_HOURS),
        "nodes": {
            "registered": nodes.get("registered", 0),
            "ready": nodes.get("ready", 0),
            "busy": nodes.get("busy", 0),
            "draining": nodes.get("draining", 0),
            "offline": nodes.get("offline", 0),
            "chat_capable": nodes.get("chat_capable", 0),
            "image_capable": nodes.get("image_capable", 0),
            "total_vram_gb": str(nodes.get("total_vram_gb", 0) or 0),
        },
        "gpus": raw.get("gpus") or [],
        "providers": {
            "total": providers.get("total", 0),
            "staked": providers.get("staked", 0),
        },
        "chat": {
            "requests_total": chat.get("requests_total", 0),
            "requests_window": chat.get("requests_window", 0),
            "tokens_total": chat.get("tokens_total", 0),
            "tokens_window": chat.get("tokens_window", 0),
            "avg_latency_ms": int(latency) if latency is not None else None,
        },
        "images": {
            "generated_total": images.get("generated_total", 0),
            "generated_window": images.get("generated_window", 0),
        },
        "models": _model_counts(),
        "generated_at": datetime.now(timezone.utc),
    }


def get_stats(db: Client) -> dict:
    """Cached network snapshot, with the live node count overlaid."""
    global _cache

    ttl = settings.NETWORK_STATS_CACHE_SECONDS
    now = time.monotonic()
    if _cache is not None and ttl > 0 and (now - _cache[0]) < ttl:
        snapshot = _cache[1]
    else:
        snapshot = _fetch(db)
        _cache = (now, snapshot)

    # Copy before mutating: the cached dict is reused across requests.
    out = {**snapshot, "nodes": {**snapshot["nodes"]}}
    out["nodes"]["online"] = len(node_manager.connected_nodes)
    return out


def reset_cache() -> None:
    """Drop the cached snapshot. Used by tests and after admin mutations."""
    global _cache
    _cache = None
