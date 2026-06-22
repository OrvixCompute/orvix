"""In-memory registry of connected nodes + job dispatch/correlation.

The orchestrator keeps node connections in memory (mirrored to the `nodes` table)
and routes inference jobs to them. Job responses arrive asynchronously over the
same WebSocket and are correlated back to the awaiting request via per-job
Futures (non-streaming) or Queues (streaming).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import AsyncIterator, Optional

from app.config import settings
from app.database import get_supabase
from app.logger import logger
from app.models.protocol import (
    JobChunkMessage,
    JobMessage,
    JobResultMessage,
    RegisterMessage,
    ShutdownMessage,
    serialize,
)

# Sentinel pushed onto a streaming queue to signal "no more chunks".
_STREAM_END = object()

JOB_TIMEOUT_S = 60.0
HEARTBEAT_STALE_S = 60.0
HEALTH_CHECK_INTERVAL_S = 30.0
# Tiers that get preferential (least-loaded) node selection.
PRIORITY_TIERS = {"gold", "diamond"}


class NodeTimeoutError(Exception):
    """A node did not return a result within the timeout."""


@dataclass
class PendingJob:
    stream: bool
    future: Optional[asyncio.Future] = None
    queue: Optional[asyncio.Queue] = None


@dataclass
class NodeConnection:
    node_id: str
    provider_id: str  # users.id of the provider
    websocket: object  # starlette WebSocket
    model: str
    gpu_info: dict
    max_concurrent_jobs: int
    status: str = "ready"  # ready | busy | draining
    current_jobs: int = 0
    last_heartbeat: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    models_supported: list[str] = field(default_factory=list)
    pending_jobs: dict[str, PendingJob] = field(default_factory=dict)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, msg) -> None:
        """Serialize and send a message, guarding against interleaved writes."""
        async with self._send_lock:
            await self.websocket.send_text(serialize(msg))


class NodeManager:
    def __init__(self) -> None:
        self.connected_nodes: dict[str, NodeConnection] = {}
        self._health_task: asyncio.Task | None = None

    # --- registration ------------------------------------------------------
    async def register_node(self, websocket, msg: RegisterMessage) -> NodeConnection:
        db = get_supabase()

        # Validate the provider exists.
        user = (
            db.table("users").select("id").eq("id", msg.provider_id).limit(1).execute()
        )
        if not user.data:
            raise ValueError("Unknown provider_id")

        # TODO: validate node_secret against users.provider_secret_hash. For now
        # we only require a non-empty secret.
        if not msg.node_secret:
            raise ValueError("Missing node_secret")

        node_id = str(uuid.uuid4())
        conn = NodeConnection(
            node_id=node_id,
            provider_id=msg.provider_id,
            websocket=websocket,
            model=msg.models_supported[0] if msg.models_supported else "",
            gpu_info=msg.gpu_info.model_dump(),
            max_concurrent_jobs=msg.max_concurrent_jobs,
            models_supported=list(msg.models_supported),
        )

        # Upsert the nodes row.
        db.table("nodes").upsert(
            {
                "id": node_id,
                "provider_id": msg.provider_id,
                "status": "ready",
                "gpu_model": msg.gpu_info.model,
                "vram_mb": msg.gpu_info.vram_total_mb,
                "models_supported": list(msg.models_supported),
                "max_concurrent_jobs": msg.max_concurrent_jobs,
                "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            }
        ).execute()

        self.connected_nodes[node_id] = conn
        logger.info(
            "Node registered: {} (provider={}, models={})",
            node_id,
            msg.provider_id,
            msg.models_supported,
        )
        return conn

    async def unregister_node(self, node_id: str) -> None:
        conn = self.connected_nodes.pop(node_id, None)
        if conn is None:
            return
        # Cancel any in-flight jobs.
        for pending in conn.pending_jobs.values():
            if pending.stream and pending.queue is not None:
                pending.queue.put_nowait(_STREAM_END)
            elif pending.future is not None and not pending.future.done():
                pending.future.set_exception(NodeTimeoutError("Node disconnected"))
        conn.pending_jobs.clear()

        try:
            get_supabase().table("nodes").update({"status": "offline"}).eq(
                "id", node_id
            ).execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to mark node {} offline: {}", node_id, exc)
        logger.info("Node unregistered: {}", node_id)

    # --- heartbeat / state -------------------------------------------------
    def update_heartbeat(self, node_id: str, status: str, current_jobs: int) -> None:
        conn = self.connected_nodes.get(node_id)
        if conn is None:
            return
        conn.status = status
        conn.current_jobs = current_jobs
        conn.last_heartbeat = datetime.now(timezone.utc)

    # --- selection ---------------------------------------------------------
    def select_node(self, model: str, user_tier: str) -> NodeConnection | None:
        candidates = [
            c
            for c in self.connected_nodes.values()
            if c.status == "ready"
            and model in c.models_supported
            and c.current_jobs < c.max_concurrent_jobs
        ]
        if not candidates:
            return None
        # Priority tiers get the least-loaded node; others get any available.
        if user_tier in PRIORITY_TIERS:
            candidates.sort(key=lambda c: c.current_jobs)
        return candidates[0]

    # --- dispatch ----------------------------------------------------------
    async def dispatch_job(self, node: NodeConnection, job: JobMessage):
        """Send a job to a node and return its result.

        Non-streaming: returns a JobResultMessage. Streaming: returns an async
        generator yielding JobChunkMessage objects.
        """
        if job.stream:
            return self._dispatch_streaming(node, job)
        return await self._dispatch_blocking(node, job)

    async def _dispatch_blocking(
        self, node: NodeConnection, job: JobMessage
    ) -> JobResultMessage:
        pending = PendingJob(stream=False, future=asyncio.get_running_loop().create_future())
        node.pending_jobs[job.job_id] = pending
        node.current_jobs += 1
        try:
            await node.send(job)
            return await asyncio.wait_for(pending.future, timeout=JOB_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise NodeTimeoutError(f"Node {node.node_id} timed out on job {job.job_id}") from exc
        finally:
            node.pending_jobs.pop(job.job_id, None)
            node.current_jobs = max(0, node.current_jobs - 1)

    async def _dispatch_streaming(
        self, node: NodeConnection, job: JobMessage
    ) -> AsyncIterator[JobChunkMessage]:
        pending = PendingJob(stream=True, queue=asyncio.Queue())
        node.pending_jobs[job.job_id] = pending
        node.current_jobs += 1
        try:
            await node.send(job)
            while True:
                item = await asyncio.wait_for(pending.queue.get(), timeout=JOB_TIMEOUT_S)
                if item is _STREAM_END:
                    break
                yield item
                if isinstance(item, JobChunkMessage) and item.is_final:
                    break
        except asyncio.TimeoutError as exc:
            raise NodeTimeoutError(
                f"Node {node.node_id} timed out streaming job {job.job_id}"
            ) from exc
        finally:
            node.pending_jobs.pop(job.job_id, None)
            node.current_jobs = max(0, node.current_jobs - 1)

    # --- response correlation (called from the WS receive loop) ------------
    def handle_job_result(self, node_id: str, msg: JobResultMessage) -> None:
        conn = self.connected_nodes.get(node_id)
        if conn is None:
            return
        pending = conn.pending_jobs.get(msg.job_id)
        if not pending:
            return
        if pending.stream:
            # A node may report a streaming failure via job_result; end the stream.
            if pending.queue is not None:
                pending.queue.put_nowait(_STREAM_END)
        elif pending.future is not None and not pending.future.done():
            pending.future.set_result(msg)

    def handle_job_chunk(self, node_id: str, msg: JobChunkMessage) -> None:
        conn = self.connected_nodes.get(node_id)
        if conn is None:
            return
        pending = conn.pending_jobs.get(msg.job_id)
        if pending and pending.queue is not None:
            pending.queue.put_nowait(msg)
            if msg.is_final:
                pending.queue.put_nowait(_STREAM_END)

    # --- provider settlement (Prompt 6) ------------------------------------
    async def settle_job(self, node: NodeConnection, cost_usdc: Decimal) -> Decimal:
        """Credit the provider their share of a completed job's cost.

        Returns the provider earning. Failures are logged, not raised — billing
        the developer already succeeded by this point.
        """
        reward_pct = Decimal(settings.PROVIDER_REWARD_PERCENTAGE) / Decimal(100)
        earning = (cost_usdc * reward_pct).quantize(Decimal("0.000001"))
        db = get_supabase()
        try:
            db.rpc(
                "credit_provider_earnings",
                {"p_user_id": node.provider_id, "p_amount": float(earning)},
            ).execute()
            # Increment the node's lifetime earnings + job counter.
            current = (
                db.table("nodes")
                .select("total_earned_usdc, total_jobs")
                .eq("id", node.node_id)
                .limit(1)
                .execute()
            )
            if current.data:
                row = current.data[0]
                db.table("nodes").update(
                    {
                        "total_earned_usdc": float(
                            Decimal(str(row["total_earned_usdc"])) + earning
                        ),
                        "total_jobs": (row["total_jobs"] or 0) + 1,
                    }
                ).eq("id", node.node_id).execute()
            logger.info(
                "Provider {} earned {} USDC for node {}",
                node.provider_id,
                earning,
                node.node_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to settle provider earning: {}", exc)
        return earning

    # --- health ------------------------------------------------------------
    async def start_health_check(self) -> None:
        self._health_task = asyncio.create_task(self._health_loop(), name="node-health")

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL_S)
            now = datetime.now(timezone.utc)
            stale = [
                nid
                for nid, c in list(self.connected_nodes.items())
                if (now - c.last_heartbeat).total_seconds() > HEARTBEAT_STALE_S
            ]
            for nid in stale:
                logger.warning("Node {} heartbeat stale — unregistering", nid)
                await self.unregister_node(nid)

    async def shutdown(self) -> None:
        if self._health_task:
            self._health_task.cancel()
        for conn in list(self.connected_nodes.values()):
            try:
                await conn.send(ShutdownMessage(reason="orchestrator shutting down"))
                await conn.websocket.close()
            except Exception:  # noqa: BLE001
                pass
        self.connected_nodes.clear()


# Process-wide singleton.
node_manager = NodeManager()
