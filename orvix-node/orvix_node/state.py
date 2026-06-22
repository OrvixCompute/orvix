"""Singleton runtime state shared across the agent, health endpoint, and executor."""

import asyncio
from datetime import datetime, timezone

from orvix_node.version import __version__


def _now() -> datetime:
    return datetime.now(timezone.utc)


class NodeState:
    """Mutable runtime state. Mutations are guarded by an asyncio.Lock."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.started_at: datetime = _now()
        self.connected_since: datetime | None = None
        self.node_id: str | None = None
        self.orchestrator_connected: bool = False
        self.gpu_status: str = "unknown"
        self.last_heartbeat: datetime | None = None

        # job_id -> brief job info
        self.current_jobs: dict[str, dict] = {}

        # counters
        self.jobs_completed: int = 0
        self.jobs_failed: int = 0
        self.total_tokens: int = 0
        self.total_earnings_usdc: float = 0.0

    # --- lifecycle ---------------------------------------------------------
    async def set_connected(self, node_id: str) -> None:
        async with self._lock:
            self.node_id = node_id
            self.orchestrator_connected = True
            self.connected_since = _now()

    async def set_disconnected(self) -> None:
        async with self._lock:
            self.orchestrator_connected = False
            self.connected_since = None

    async def mark_heartbeat(self) -> None:
        async with self._lock:
            self.last_heartbeat = _now()

    async def set_gpu_status(self, status: str) -> None:
        async with self._lock:
            self.gpu_status = status

    # --- jobs --------------------------------------------------------------
    async def add_job(self, job_id: str, info: dict) -> None:
        async with self._lock:
            self.current_jobs[job_id] = info

    async def remove_job(self, job_id: str) -> None:
        async with self._lock:
            self.current_jobs.pop(job_id, None)

    async def record_completed(self, total_tokens: int, earnings: float = 0.0) -> None:
        async with self._lock:
            self.jobs_completed += 1
            self.total_tokens += total_tokens
            self.total_earnings_usdc += earnings

    async def record_failed(self) -> None:
        async with self._lock:
            self.jobs_failed += 1

    # --- snapshots ---------------------------------------------------------
    def uptime_seconds(self) -> float:
        return (_now() - self.started_at).total_seconds()

    def health_snapshot(self) -> dict:
        return {
            "version": __version__,
            "uptime": round(self.uptime_seconds(), 1),
            "current_jobs": len(self.current_jobs),
            "gpu_status": self.gpu_status,
            "orchestrator_connected": self.orchestrator_connected,
            "node_id": self.node_id,
        }

    def metrics_snapshot(self) -> dict:
        return {
            "jobs_completed": self.jobs_completed,
            "jobs_failed": self.jobs_failed,
            "total_tokens": self.total_tokens,
            "total_earnings_usdc": round(self.total_earnings_usdc, 6),
            "current_jobs": len(self.current_jobs),
        }


# Process-wide singleton.
state = NodeState()
