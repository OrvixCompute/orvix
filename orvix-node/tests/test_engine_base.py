"""Tests for the engine hierarchy: metadata + the ChatEngine lifecycle bridge.

The lifecycle methods (load/unload/is_loaded) used by the future ModelManager
must delegate to the existing chat contract (initialize/is_ready/shutdown) so
chat behavior is unchanged.
"""

from __future__ import annotations

from typing import AsyncIterator

from orvix_node.inference.base import (
    AbstractEngine,
    ChatEngine,
    GenerateChunk,
    GenerateRequest,
    GenerateResponse,
)
from orvix_node.inference.mock import MockBackend
from orvix_node.inference.vllm import VLLMBackend


class _RecordingChat(ChatEngine):
    required_vram_gb = 5.0
    supported_models = ["x"]

    def __init__(self):
        self.events: list = []
        self.model = "m1"
        self._ready = False

    async def initialize(self, model: str) -> None:
        self.events.append(("init", model))
        self._ready = True

    async def is_ready(self) -> bool:
        return self._ready

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        return GenerateResponse(content="", prompt_tokens=0, completion_tokens=0)

    async def generate_stream(
        self, request: GenerateRequest
    ) -> AsyncIterator[GenerateChunk]:
        if False:  # pragma: no cover — empty async generator
            yield GenerateChunk()

    async def shutdown(self) -> None:
        self.events.append(("shutdown",))
        self._ready = False


async def test_chat_lifecycle_delegates_to_contract():
    e = _RecordingChat()
    assert e.engine_type == "chat"
    assert await e.is_loaded() is False

    await e.load()
    assert ("init", "m1") in e.events
    assert await e.is_loaded() is True

    await e.unload()
    assert ("shutdown",) in e.events
    assert await e.is_loaded() is False


def test_concrete_engines_are_abstract_engines():
    assert issubclass(VLLMBackend, ChatEngine)
    assert issubclass(MockBackend, ChatEngine)
    assert issubclass(ChatEngine, AbstractEngine)


def test_vllm_metadata():
    assert VLLMBackend.engine_type == "chat"
    assert VLLMBackend.required_vram_gb == 18.0
    assert VLLMBackend.supported_models == ["qwen-2.5-7b"]


async def test_mock_backend_is_a_chat_engine():
    b = MockBackend("p")
    assert b.engine_type == "chat"
    assert await b.is_loaded() is False
    await b.initialize("qwen-2.5-7b")
    assert await b.is_loaded() is True
    assert b.model == "qwen-2.5-7b"
