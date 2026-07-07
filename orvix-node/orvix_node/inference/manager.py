"""ModelManager — keeps up to ``max_resident`` engines in a single GPU's VRAM,
loading (and evicting, LRU, when at capacity) on demand.

Default (``max_resident=1``): only one engine is resident at a time — every
request for a different engine unloads the current one first (a "swap"). This
is the original single-GPU behavior for engines that can't coexist (e.g. an
fp16 7B chat model + an image model that together exceed VRAM).

``max_resident >= len(engines)`` (e.g. 2 for chat+image): every engine can stay
resident at once — a request for an engine that isn't loaded yet just loads it
into a free slot, without touching the others. Nothing is ever evicted unless
capacity is actually exceeded. This is what makes concurrent chat+image serving
possible on one GPU once the models are small enough to fit together (e.g. an
AWQ-quantized chat model alongside an image engine).

Concurrency (single ``asyncio.Condition`` guarding all state):
  - Request for an already-loaded engine → served immediately (fast path).
  - Request for an engine that needs loading, with a free slot → load it; other
    already-resident engines are untouched.
  - Request for an engine that needs loading, at capacity → evict the
    least-recently-used *idle* (no in-flight jobs) resident engine, then load.
    If every resident engine is busy, wait for one to drain.
  - Several waiters for the same target share one load.

Lock ordering: the condition's lock is the only lock. ``load``/``unload`` are
awaited while holding it, but that is safe because an eviction only begins once
the outgoing engine is fully drained (no in-flight job can call :meth:`release`
mid-eviction), so nothing contends for the lock mid-transition.

Idle unload frees VRAM per-engine after a period of inactivity so a
mixed-traffic node does not pin VRAM on engines that ran once and went quiet.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable, Dict, Optional

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
        max_resident: int = 1,
    ) -> None:
        self._engines = engines
        self.idle_timeout_seconds = idle_timeout_seconds
        self._clock = clock
        self.max_resident = max_resident

        self._cv = asyncio.Condition()
        self._loaded: set[str] = set()  # engine_types currently resident in VRAM
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
            while not (engine_type in self._loaded and not self._swapping):
                if self._swapping:
                    await self._cv.wait()
                    continue
                if len(self._loaded) < self.max_resident:
                    await self._load_locked(engine_type, model_id, evict=None)
                    continue
                victim = self._pick_victim(engine_type)
                if victim is None:
                    # Every resident engine (at capacity) is busy — wait to drain.
                    await self._cv.wait()
                    continue
                await self._load_locked(engine_type, model_id, evict=victim)
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
        """Unload any resident engine that has been idle past the timeout."""
        async with self._cv:
            if self._swapping:
                return
            now = self._clock()
            for engine_type in list(self._loaded):
                if self._active.get(engine_type, 0) > 0:
                    continue
                idle = now - self._last_used.get(engine_type, now)
                if idle <= self.idle_timeout_seconds:
                    continue
                logger.info(
                    "Idle-unloading {} after {:.0f}s of inactivity", engine_type, idle
                )
                self._swapping = True
                try:
                    await self._engines[engine_type].unload()
                    self._loaded.discard(engine_type)
                finally:
                    self._swapping = False
                    self._cv.notify_all()

    async def shutdown(self) -> None:
        async with self._cv:
            while self._swapping:
                await self._cv.wait()
            for engine_type in list(self._loaded):
                await self._engines[engine_type].unload()
            self._loaded.clear()
            self._cv.notify_all()

    def status(self) -> dict:
        now = self._clock()
        loaded = sorted(self._loaded)
        return {
            # Preserved for single-engine (max_resident=1) callers: the one
            # resident engine, or None. Ambiguous (and thus None) once more
            # than one engine is resident — see loaded_engines for that case.
            "current_engine": loaded[0] if len(loaded) == 1 else None,
            "loaded_engines": loaded,
            "swapping": self._swapping,
            "engines": list(self._engines),
            "active_jobs": dict(self._active),
            "idle_seconds": {
                e: round(now - self._last_used[e], 1) for e in loaded if e in self._last_used
            },
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "swaps_last_minute": self._swaps_in_window(now),
        }

    # --- internals (must hold self._cv) -----------------------------------
    def _pick_victim(self, engine_type: str) -> Optional[str]:
        """Least-recently-used *idle* resident engine, or None if all are busy."""
        candidates = [
            e for e in self._loaded if e != engine_type and self._active.get(e, 0) == 0
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda e: self._last_used.get(e, 0.0))

    async def _load_locked(
        self, engine_type: str, model_id: str, evict: Optional[str]
    ) -> None:
        self._swapping = True
        try:
            if evict is not None:
                logger.info("Unloading {} to free VRAM for {}", evict, engine_type)
                await self._engines[evict].unload()
                self._loaded.discard(evict)
            self._record_swap()
            logger.info("Loading {} ({})", engine_type, model_id)
            load_start = self._clock()
            await self._engines[engine_type].load(model_id)
            self._loaded.add(engine_type)
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
