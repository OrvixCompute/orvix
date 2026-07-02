"""Persistent WebSocket client to the orchestrator: register, heartbeat, receive
jobs, auto-reconnect with exponential backoff.
"""

from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable
from urllib.parse import urlparse

import websockets
from websockets.exceptions import ConnectionClosed

from orvix_node.config import NodeConfig
from orvix_node.exceptions import AuthError
from orvix_node.gpu import detector
from orvix_node.inference.router import available_engine_types
from orvix_node.logger import logger
from orvix_node.protocol import (
    BaseMessage,
    GPUInfo,
    HeartbeatMessage,
    ImageJobDispatchMessage,
    JobMessage,
    RegisterMessage,
    parse_message,
    serialize,
)
from orvix_node.state import state
from orvix_node.version import __version__

JobHandler = Callable[[JobMessage], Awaitable[None]]
ImageHandler = Callable[[ImageJobDispatchMessage], Awaitable[None]]

ACK_TIMEOUT = 10.0
MAX_BACKOFF = 60.0


class OrchestratorClient:
    def __init__(self, config: NodeConfig) -> None:
        self.config = config
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._outbound: asyncio.Queue[BaseMessage] = asyncio.Queue()
        self._job_handler: JobHandler | None = None
        self._image_handler: ImageHandler | None = None
        self._stop = asyncio.Event()
        self._draining = False
        self._connected = False
        self._job_tasks: set[asyncio.Task] = set()

    # --- public API --------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self._connected

    def set_job_handler(self, callback: JobHandler) -> None:
        self._job_handler = callback

    def set_image_handler(self, callback: ImageHandler) -> None:
        self._image_handler = callback

    async def send_message(self, msg: BaseMessage) -> None:
        await self._outbound.put(msg)

    async def stop(self) -> None:
        """Graceful shutdown: send draining status, let jobs finish, close."""
        logger.info("Client stopping (draining)...")
        self._draining = True
        self._stop.set()
        if self._ws is not None:
            try:
                await self.send_message(
                    HeartbeatMessage(
                        status="draining",
                        current_jobs=len(state.current_jobs),
                        gpu_metrics=detector.get_metrics(),
                    )
                )
                await asyncio.sleep(0.2)  # give the sender a moment
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    # --- main loop ---------------------------------------------------------
    def _connect_url(self) -> str:
        base = self.config.orchestrator_url.rstrip("/")
        return f"{base}/v1/node/connect"

    def _check_transport_security(self) -> None:
        url = self.config.orchestrator_url
        scheme = urlparse(url).scheme
        host = urlparse(url).hostname or ""
        if scheme == "ws":
            if host in ("localhost", "127.0.0.1"):
                logger.warning("DEV MODE: using insecure ws:// to {} (localhost only)", host)
            elif os.environ.get("ORVIX_NODE_ALLOW_INSECURE_WS", "").strip().lower() in (
                "1",
                "true",
                "yes",
            ):
                logger.warning(
                    "INSECURE: using ws:// to non-local host '{}' "
                    "(ORVIX_NODE_ALLOW_INSECURE_WS set). Use wss:// in production.",
                    host,
                )
            else:
                raise AuthError(
                    f"Refusing insecure ws:// to non-local host '{host}'. Use wss://, "
                    "or set ORVIX_NODE_ALLOW_INSECURE_WS=true to override (dev only)."
                )

    async def start(self) -> None:
        """Run the connect/reconnect loop until stop() is called or auth fails."""
        self._check_transport_security()
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._run_once()
                backoff = 1.0  # clean exit (stop requested) — reset
            except AuthError:
                raise  # non-retryable, propagate to caller (exits process)
            except ConnectionClosed as exc:
                logger.warning("Connection closed: {}", exc)
            except (OSError, websockets.exceptions.InvalidURI) as exc:
                logger.warning("Connection error: {}", exc)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Unexpected client error: {}", exc)

            await state.set_disconnected()
            self._connected = False
            if self._stop.is_set():
                break
            logger.info("Reconnecting in {:.0f}s...", backoff)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def _run_once(self) -> None:
        url = self._connect_url()
        logger.info("Connecting to {}", url)
        # ping_interval=None: rely on the app-level heartbeat (sent every
        # heartbeat_interval s) for liveness instead of WS keepalive pings, which
        # were dropping the connection over higher-latency links (1011 timeouts).
        async with websockets.connect(
            url, max_size=8 * 1024 * 1024, ping_interval=None
        ) as ws:
            self._ws = ws
            await self._register(ws)

            sender = asyncio.create_task(self._sender_loop(ws), name="sender")
            heartbeat = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
            receiver = asyncio.create_task(self._receiver_loop(ws), name="receiver")
            tasks = [sender, heartbeat, receiver]
            try:
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
                # Surface any exception (e.g. ConnectionClosed) to start().
                for t in done:
                    exc = t.exception()
                    if exc:
                        raise exc
            finally:
                for t in tasks:
                    t.cancel()
                self._ws = None

    # --- handshake ---------------------------------------------------------
    async def _register(self, ws) -> None:
        gpu = detector.detect() or GPUInfo(model="unknown", vram_total_mb=0)
        reg = RegisterMessage(
            provider_id=self.config.provider_id,
            node_secret=self.config.node_secret,
            version=__version__,
            gpu_info=gpu,
            models_supported=[self.config.model],
            max_concurrent_jobs=self.config.max_concurrent_jobs,
            engines=available_engine_types(self.config.enable_image_engine),
            vram_gb=round(gpu.vram_total_mb / 1024, 1) if gpu.vram_total_mb else 0.0,
        )
        await ws.send(serialize(reg))

        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=ACK_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise ConnectionError("Timed out waiting for register_ack") from exc

        ack = parse_message(raw)
        if getattr(ack, "type", None) != "register_ack":
            raise ConnectionError(f"Expected register_ack, got {getattr(ack, 'type', '?')}")
        if not ack.accepted:
            raise AuthError(f"Registration rejected: {ack.reason}")

        await state.set_connected(ack.node_id)
        self._connected = True
        logger.info("Registered with orchestrator as node {}", ack.node_id)

    # --- loops -------------------------------------------------------------
    async def _sender_loop(self, ws) -> None:
        while True:
            msg = await self._outbound.get()
            await ws.send(serialize(msg))

    async def _heartbeat_loop(self) -> None:
        while True:
            status = (
                "draining"
                if self._draining
                else ("busy" if len(state.current_jobs) >= self.config.max_concurrent_jobs else "ready")
            )
            await self.send_message(
                HeartbeatMessage(
                    status=status,
                    current_jobs=len(state.current_jobs),
                    gpu_metrics=detector.get_metrics(),
                )
            )
            await state.mark_heartbeat()
            await asyncio.sleep(self.config.heartbeat_interval)

    async def _receiver_loop(self, ws) -> None:
        async for raw in ws:
            try:
                msg = parse_message(raw)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not parse inbound message: {}", exc)
                continue
            await self._route(msg)

    async def _route(self, msg: BaseMessage) -> None:
        mtype = getattr(msg, "type", None)
        if mtype == "job":
            await self._dispatch_job(msg)  # type: ignore[arg-type]
        elif mtype == "job.image.dispatch":
            await self._dispatch_image(msg)  # type: ignore[arg-type]
        elif mtype == "ping":
            await self.send_message(
                HeartbeatMessage(
                    status="ready",
                    current_jobs=len(state.current_jobs),
                    gpu_metrics=detector.get_metrics(),
                )
            )
        elif mtype == "shutdown":
            logger.info("Shutdown requested by orchestrator: {}", getattr(msg, "reason", ""))
            await self.stop()
        else:
            logger.debug("Ignoring inbound message of type {}", mtype)

    async def _dispatch_job(self, job: JobMessage) -> None:
        if self._job_handler is None:
            logger.warning("Received job {} but no handler registered", job.job_id)
            return
        # Run in the background so the receive loop keeps flowing.
        task = asyncio.create_task(self._job_handler(job), name=f"job-{job.job_id}")
        self._job_tasks.add(task)
        task.add_done_callback(self._job_tasks.discard)

    async def _dispatch_image(self, job: ImageJobDispatchMessage) -> None:
        if self._image_handler is None:
            logger.warning("Received image job {} but no image handler registered", job.job_id)
            return
        task = asyncio.create_task(self._image_handler(job), name=f"image-{job.job_id}")
        self._job_tasks.add(task)
        task.add_done_callback(self._job_tasks.discard)
