"""Local FastAPI health/metrics server, run programmatically inside the agent's
event loop via uvicorn.
"""

import asyncio

import uvicorn
from fastapi import FastAPI

from orvix_node.gpu import detector
from orvix_node.logger import logger
from orvix_node.state import state
from orvix_node.version import __version__


def create_health_app() -> FastAPI:
    app = FastAPI(title="Orvix Node", version=__version__)

    @app.get("/health")
    async def health() -> dict:
        snap = state.health_snapshot()
        snap["status"] = "ok"
        snap["gpu"] = detector.health_check()
        return snap

    @app.get("/metrics")
    async def metrics() -> dict:
        data = state.metrics_snapshot()
        data["gpu"] = detector.get_metrics().model_dump(mode="json")
        return data

    return app


class HealthServer:
    """Runs the health app as a background uvicorn server in the current loop."""

    def __init__(self, port: int, host: str = "127.0.0.1") -> None:
        config = uvicorn.Config(
            create_health_app(),
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._task: asyncio.Task | None = None
        self._port = port

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve(), name="health-server")
        logger.info("Health endpoint on http://127.0.0.1:{}/health", self._port)

    async def stop(self) -> None:
        self._server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
        logger.info("Health endpoint stopped")
