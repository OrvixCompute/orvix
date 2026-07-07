"""Tests for the ModelManager: swap, drain-before-unload, idle unload (fake
clock), concurrency, thrash counting, and shutdown."""

from __future__ import annotations

import asyncio

import pytest

from orvix_node.inference.base import AbstractEngine
from orvix_node.inference.manager import ModelManager


class FakeEngine(AbstractEngine):
    def __init__(self, engine_type: str):
        self.engine_type = engine_type  # shadow the ClassVar per instance
        self.loads: list[str] = []
        self.unloads = 0
        self._loaded = False

    async def load(self, model_id: str) -> None:
        self.loads.append(model_id)
        self._loaded = True

    async def unload(self) -> None:
        self.unloads += 1
        self._loaded = False

    async def is_loaded(self) -> bool:
        return self._loaded


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


async def test_acquire_loads_and_tracks_current():
    chat = FakeEngine("chat")
    mgr = ModelManager({"chat": chat})
    engine = await mgr.acquire("qwen-2.5-7b")
    assert engine is chat
    assert chat.loads == ["qwen-2.5-7b"]
    assert mgr.status()["current_engine"] == "chat"
    await mgr.release("chat")


async def test_fast_path_no_reload():
    chat = FakeEngine("chat")
    mgr = ModelManager({"chat": chat})
    async with mgr.serving("qwen-2.5-7b"):
        pass
    async with mgr.serving("qwen-2.5-7b"):
        pass
    assert chat.loads == ["qwen-2.5-7b"]  # loaded exactly once


async def test_swap_unloads_previous():
    chat, img = FakeEngine("chat"), FakeEngine("image")
    mgr = ModelManager({"chat": chat, "image": img})
    async with mgr.serving("qwen-2.5-7b"):
        pass
    async with mgr.serving("flux-schnell"):
        pass
    assert chat.unloads == 1
    assert img.loads == ["flux-schnell"]
    assert mgr.status()["current_engine"] == "image"


async def test_swap_waits_for_drain():
    chat, img = FakeEngine("chat"), FakeEngine("image")
    mgr = ModelManager({"chat": chat, "image": img})

    # Hold a chat job in flight.
    await mgr.acquire("qwen-2.5-7b")  # active[chat] = 1

    # An image request must wait until the chat job drains before swapping.
    img_task = asyncio.create_task(mgr.acquire("flux-schnell"))
    await asyncio.sleep(0.05)
    assert not img_task.done()
    assert chat.unloads == 0  # not swapped while chat job in flight

    await mgr.release("chat")  # drain
    engine = await asyncio.wait_for(img_task, timeout=1)
    assert engine is img
    assert chat.unloads == 1
    await mgr.release("image")


async def test_concurrent_same_engine_shares_load():
    chat = FakeEngine("chat")
    mgr = ModelManager({"chat": chat})
    e1 = await mgr.acquire("qwen-2.5-7b")
    e2 = await mgr.acquire("qwen-2.5-7b")
    assert e1 is e2 is chat
    assert mgr.status()["active_jobs"]["chat"] == 2
    assert chat.loads == ["qwen-2.5-7b"]  # single load
    await mgr.release("chat")
    await mgr.release("chat")


async def test_idle_unload_after_timeout():
    clock = Clock()
    chat = FakeEngine("chat")
    mgr = ModelManager({"chat": chat}, idle_timeout_seconds=600, clock=clock)
    async with mgr.serving("qwen-2.5-7b"):
        pass

    await mgr.idle_check()  # not idle yet
    assert mgr.status()["current_engine"] == "chat"

    clock.advance(601)
    await mgr.idle_check()
    assert mgr.status()["current_engine"] is None
    assert chat.unloads == 1


async def test_idle_unload_skipped_while_active():
    clock = Clock()
    chat = FakeEngine("chat")
    mgr = ModelManager({"chat": chat}, idle_timeout_seconds=600, clock=clock)
    await mgr.acquire("qwen-2.5-7b")  # active, not released
    clock.advance(601)
    await mgr.idle_check()
    assert mgr.status()["current_engine"] == "chat"  # kept — job in flight
    await mgr.release("chat")


async def test_shutdown_unloads_current():
    chat = FakeEngine("chat")
    mgr = ModelManager({"chat": chat})
    async with mgr.serving("qwen-2.5-7b"):
        pass
    await mgr.shutdown()
    assert chat.unloads == 1
    assert mgr.status()["current_engine"] is None


async def test_thrash_counter():
    clock = Clock()
    chat, img = FakeEngine("chat"), FakeEngine("image")
    mgr = ModelManager({"chat": chat, "image": img}, clock=clock)
    for model in ["qwen-2.5-7b", "flux-schnell", "qwen-2.5-7b", "flux-schnell"]:
        async with mgr.serving(model):
            pass
    assert mgr.status()["swaps_last_minute"] == 4


async def test_unknown_model_raises():
    mgr = ModelManager({"chat": FakeEngine("chat")})
    with pytest.raises(ValueError, match="Unknown model"):
        await mgr.acquire("does-not-exist")


async def test_missing_engine_type_raises():
    mgr = ModelManager({"chat": FakeEngine("chat")})  # no image engine
    with pytest.raises(ValueError, match="No engine registered"):
        await mgr.acquire("flux-schnell")


# --- concurrent mode (max_resident >= number of engines) -------------------


async def test_concurrent_mode_keeps_both_resident_no_eviction():
    chat, img = FakeEngine("chat"), FakeEngine("image")
    mgr = ModelManager({"chat": chat, "image": img}, max_resident=2)
    async with mgr.serving("qwen-2.5-7b"):
        pass
    async with mgr.serving("flux-schnell"):
        pass
    # Loading image must NOT have evicted chat — both fit within max_resident.
    assert chat.unloads == 0
    assert img.unloads == 0
    assert sorted(mgr.status()["loaded_engines"]) == ["chat", "image"]

    # Both are fast-path now: neither reloads on a repeat request.
    async with mgr.serving("qwen-2.5-7b"):
        pass
    async with mgr.serving("flux-schnell"):
        pass
    assert chat.loads == ["qwen-2.5-7b"]
    assert img.loads == ["flux-schnell"]


async def test_concurrent_mode_serves_both_simultaneously():
    chat, img = FakeEngine("chat"), FakeEngine("image")
    mgr = ModelManager({"chat": chat, "image": img}, max_resident=2)
    chat_engine = await mgr.acquire("qwen-2.5-7b")
    # While a chat job is in flight (not released), an image request must NOT
    # block on it — they coexist.
    img_engine = await asyncio.wait_for(mgr.acquire("flux-schnell"), timeout=1)
    assert chat_engine is chat
    assert img_engine is img
    assert mgr.status()["active_jobs"] == {"chat": 1, "image": 1}
    await mgr.release("chat")
    await mgr.release("image")


async def test_concurrent_mode_evicts_lru_when_over_capacity(monkeypatch):
    import orvix_node.inference.manager as manager_mod

    a, b, c = FakeEngine("a"), FakeEngine("b"), FakeEngine("c")
    mgr = ModelManager({"a": a, "b": b, "c": c}, max_resident=2)
    monkeypatch.setattr(
        manager_mod,
        "engine_type_for",
        {"model-a": "a", "model-b": "b", "model-c": "c"}.__getitem__,
    )

    async with mgr.serving("model-a"):
        pass
    async with mgr.serving("model-b"):
        pass
    assert sorted(mgr.status()["loaded_engines"]) == ["a", "b"]

    # At capacity (2/2); "a" is the least-recently-used idle engine, so
    # requesting "c" must evict "a", not "b".
    async with mgr.serving("model-c"):
        pass
    assert a.unloads == 1
    assert b.unloads == 0
    assert sorted(mgr.status()["loaded_engines"]) == ["b", "c"]


async def test_idle_check_unloads_each_engine_independently():
    clock = Clock()
    chat, img = FakeEngine("chat"), FakeEngine("image")
    mgr = ModelManager(
        {"chat": chat, "image": img}, idle_timeout_seconds=600, clock=clock, max_resident=2
    )
    async with mgr.serving("qwen-2.5-7b"):
        pass
    clock.advance(300)
    async with mgr.serving("flux-schnell"):
        pass

    clock.advance(301)  # chat now idle 601s, image idle 301s
    await mgr.idle_check()
    assert chat.unloads == 1
    assert img.unloads == 0
    assert mgr.status()["loaded_engines"] == ["image"]

    clock.advance(300)  # image now idle 601s
    await mgr.idle_check()
    assert img.unloads == 1
    assert mgr.status()["loaded_engines"] == []


async def test_shutdown_unloads_all_resident_engines():
    chat, img = FakeEngine("chat"), FakeEngine("image")
    mgr = ModelManager({"chat": chat, "image": img}, max_resident=2)
    async with mgr.serving("qwen-2.5-7b"):
        pass
    async with mgr.serving("flux-schnell"):
        pass
    await mgr.shutdown()
    assert chat.unloads == 1
    assert img.unloads == 1
    assert mgr.status()["loaded_engines"] == []
