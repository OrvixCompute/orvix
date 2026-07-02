"""ModelManager — swaps engines in and out of a single GPU's VRAM on demand.

Only one engine is resident at a time. When a job needs a different engine, the
manager unloads the current one (freeing VRAM) and loads the requested one.

Concurrency (single ``asyncio.Condition`` guarding all state):
  - Request for the currently-loaded engine → served immediately (fast path).
  - Request for a different engine → the manager waits until the outgoing engine
    has no in-flight jobs (drain), then swaps. New arrivals queue behind the swap.
  - Several waiters for the same target share one swap.
  - A request for the current engine that arrives *during* a swap away from it
    waits for the swap to finish, then triggers a swap back (expensive — logged
    via the thrash counter).

Lock ordering: the condition's lock is the only lock. ``load``/``unload`` are
awaited while holding it, but that is safe because a swap only begins once the
outgoing engine is fully drained (no in-flight job can call :meth:`release`
during the swap), so nothing contends for the lock mid-swap.

Idle unload frees VRAM after a period of inactivity so a mixed-traffic node does
not pin the GPU on whichever engine ran last.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable, Dict

from orvix_node.inference.base import AbstractEngine
from orvix_node.inference.router import engine_type_for
from orvix_node.logger import logger

_THRASH_WINDOW_SECONDS = 60.0
_THRASH_THRESHOLD = 3  # >3 swaps/min is flagged (not auto-mitigated)


class ModelManager:
    def __init__(
        self,
        engines: Dict[str, AbstractEngine],
        idle_timeout_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engines = engines
        self.idle_timeout_seconds = idle_timeout_seconds
        self._clock = clock

        self._cv = asyncio.Condition()
        self._current: str | None = None  # engine_type currently in VRAM
        self._swapping = False
        self._active: Dict[str, int] = {}  # engine_type -> in-flight job count
        self._last_used: Dict[str, float] = {}
        self._swap_times: list[float] = []

    # --- public API --------------------------------------------------------
    async def acquire(self, model_id: str) -> AbstractEngine:
        """Ensure the engine serving ``model_id`` is loaded and reserve it.

        The caller MUST pair this with :meth:`release` (use :meth:`serving`).
        """
        engine_type = engine_type_for(model_id)
        if engine_type not in self._engines:
            raise ValueError(
                f"No engine registered for type {engine_type!r} (model {model_id!r})"
            )
        async with self._cv:
            while not (self._current == engine_type and not self._swapping):
                if self._swapping:
                    await self._cv.wait()
                    continue
                outgoing = self._current
                if (
                    outgoing is not None
                    and outgoing != engine_type
                    and self._active.get(outgoing, 0) > 0
                ):
                    # Wait for in-flight jobs on the outgoing engine to drain.
                    await self._cv.wait()
                    continue
                await self._swap_locked(engine_type, model_id, outgoing)
            self._active[engine_type] = self._active.get(engine_type, 0) + 1
            self._last_used[engine_type] = self._clock()
            return self._engines[engine_type]

    async def release(self, engine_type: str) -> None:
        async with self._cv:
            if self._active.get(engine_type, 0) > 0:
                self._active[engine_type] -= 1
            self._last_used[engine_type] = self._clock()
            self._cv.notify_all()

    @asynccontextmanager
    async def serving(self, model_id: str) -> AsyncIterator[AbstractEngine]:
        """Acquire the engine for ``model_id`` for the duration of the block."""
        engine = await self.acquire(model_id)
        try:
            yield engine
        finally:
            await self.release(engine.engine_type)

    async def idle_check(self) -> None:
        """Unload the resident engine if it has been idle past the timeout."""
        async with self._cv:
            if self._current is None or self._swapping:
                return
            if self._active.get(self._current, 0) > 0:
                return
            idle = self._clock() - self._last_used.get(self._current, self._clock())
            if idle <= self.idle_timeout_seconds:
                return
            engine_type = self._current
            logger.info(
                "Idle-unloading {} after {:.0f}s of inactivity", engine_type, idle
            )
            self._swapping = True
            try:
                await self._engines[engine_type].unload()
                self._current = None
            finally:
                self._swapping = False
                self._cv.notify_all()

    async def shutdown(self) -> None:
        async with self._cv:
            while self._swapping:
                await self._cv.wait()
            if self._current is not None:
                await self._engines[self._current].unload()
                self._current = None
            self._cv.notify_all()

    def status(self) -> dict:
        now = self._clock()
        return {
            "current_engine": self._current,
            "swapping": self._swapping,
            "engines": list(self._engines),
            "active_jobs": dict(self._active),
            "idle_seconds": (
                round(now - self._last_used[self._current], 1)
                if self._current and self._current in self._last_used
                else None
            ),
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "swaps_last_minute": self._swaps_in_window(now),
        }

    # --- internals (must hold self._cv) -----------------------------------
    async def _swap_locked(
        self, engine_type: str, model_id: str, outgoing: str | None
    ) -> None:
        self._swapping = True
        try:
            if outgoing is not None:
                logger.info("Unloading {} to free VRAM for {}", outgoing, engine_type)
                await self._engines[outgoing].unload()
                self._current = None
            self._record_swap()
            logger.info("Loading {} ({})", engine_type, model_id)
            load_start = self._clock()
            await self._engines[engine_type].load(model_id)
            self._current = engine_type
            logger.info(
                "Loaded {} in {:.1f}s", engine_type, self._clock() - load_start
            )
        finally:
            self._swapping = False
            self._cv.notify_all()

    def _record_swap(self) -> None:
        now = self._clock()
        self._swap_times.append(now)
        self._swap_times = [t for t in self._swap_times if now - t <= _THRASH_WINDOW_SECONDS]
        if len(self._swap_times) > _THRASH_THRESHOLD:
            logger.warning(
                "Engine thrashing: {} swaps in the last minute (mixed traffic on a "
                "single GPU). Not auto-mitigating.",
                len(self._swap_times),
            )

    def _swaps_in_window(self, now: float) -> int:
        return len([t for t in self._swap_times if now - t <= _THRASH_WINDOW_SECONDS])
