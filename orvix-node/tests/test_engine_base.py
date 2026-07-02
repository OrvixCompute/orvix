"""Tests for the engine hierarchy: metadata + the unified lifecycle.

Every engine shares load(model_id)/unload/is_loaded; chat engines add
generate/generate_stream, image engines add infer.
"""

from __future__ import annotations

from typing import AsyncIterator

from orvix_node.inference.base import (
    AbstractEngine,
    ChatEngine,
    GenerateChunk,
    GenerateRequest,
    GenerateResponse,
    ImageEngine,
)
from orvix_node.inference.mock import MockBackend
from orvix_node.inference.vllm import VLLMBackend


class _RecordingChat(ChatEngine):
    required_vram_gb = 5.0
    supported_models = ["x"]

    def __init__(self):
        self.events: list = []
        self._loaded = False

    async def load(self, model_id: str) -> None:
        self.events.append(("load", model_id))
        self._loaded = True

    async def unload(self) -> None:
        self.events.append(("unload",))
        self._loaded = False

    async def is_loaded(self) -> bool:
        return self._loaded

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        return GenerateResponse(content="", prompt_tokens=0, completion_tokens=0)

    async def generate_stream(
        self, request: GenerateRequest
    ) -> AsyncIterator[GenerateChunk]:
        if False:  # pragma: no cover — empty async generator
            yield GenerateChunk()


async def test_chat_lifecycle():
    e = _RecordingChat()
    assert e.engine_type == "chat"
    assert await e.is_loaded() is False

    await e.load("qwen-2.5-7b")
    assert ("load", "qwen-2.5-7b") in e.events
    assert await e.is_loaded() is True

    await e.unload()
    assert ("unload",) in e.events
    assert await e.is_loaded() is False


def test_class_hierarchy():
    assert issubclass(VLLMBackend, ChatEngine)
    assert issubclass(MockBackend, ChatEngine)
    assert issubclass(ChatEngine, AbstractEngine)
    assert issubclass(ImageEngine, AbstractEngine)


def test_vllm_metadata():
    assert VLLMBackend.engine_type == "chat"
    assert VLLMBackend.required_vram_gb == 18.0
    assert VLLMBackend.supported_models == ["qwen-2.5-7b"]


async def test_mock_backend_lifecycle():
    b = MockBackend("p")
    assert b.engine_type == "chat"
    assert await b.is_loaded() is False
    await b.load("qwen-2.5-7b")
    assert await b.is_loaded() is True
    assert b.model == "qwen-2.5-7b"
    await b.unload()
    assert await b.is_loaded() is False
