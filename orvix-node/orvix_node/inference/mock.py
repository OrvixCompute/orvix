"""Mock inference backend — returns fake responses so the full job pipeline can
be tested without a GPU. Token estimation uses tiktoken for realism.
"""

from __future__ import annotations

import asyncio
import random
from typing import AsyncIterator

from orvix_node.inference.base import (
    GenerateChunk,
    GenerateRequest,
    GenerateResponse,
    GenerateUsage,
)
from orvix_node.logger import logger

try:
    import tiktoken

    _enc = tiktoken.get_encoding("cl100k_base")
except Exception:  # noqa: BLE001 — tiktoken optional; fall back to whitespace
    _enc = None


def _count_tokens(text: str) -> int:
    if _enc is not None:
        return len(_enc.encode(text))
    return max(1, len(text.split()))


def _last_user(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content", ""))
    return str(messages[-1].get("content", "")) if messages else ""


class MockBackend:
    def __init__(self, provider_id: str = "local") -> None:
        self.provider_id = provider_id
        self._model: str | None = None
        self._ready = False

    async def initialize(self, model: str) -> None:
        logger.info("Mock backend initialized for model {}", model)
        await asyncio.sleep(1.0)  # simulate load time
        self._model = model
        self._ready = True

    async def is_ready(self) -> bool:
        return self._ready

    def _make_content(self, messages: list[dict]) -> str:
        snippet = _last_user(messages)[:80]
        return (
            f"This is a mock response from Orvix Node {self.provider_id}. "
            f"You asked: {snippet}..."
        )

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        await asyncio.sleep(random.uniform(0.2, 0.8))
        content = self._make_content(request.messages)
        prompt_tokens = sum(_count_tokens(str(m.get("content", ""))) for m in request.messages)
        completion_tokens = random.randint(50, 200)
        return GenerateResponse(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason="stop",
        )

    async def generate_stream(
        self, request: GenerateRequest
    ) -> AsyncIterator[GenerateChunk]:
        content = self._make_content(request.messages)
        prompt_tokens = sum(_count_tokens(str(m.get("content", ""))) for m in request.messages)

        if _enc is not None:
            token_ids = _enc.encode(content)
            pieces = [_enc.decode(token_ids[i : i + 5]) for i in range(0, len(token_ids), 5)]
            completion_tokens = len(token_ids)
        else:
            words = content.split()
            pieces = [" ".join(words[i : i + 3]) + " " for i in range(0, len(words), 3)]
            completion_tokens = len(words)

        for piece in pieces:
            await asyncio.sleep(0.05)
            yield GenerateChunk(delta_content=piece, is_final=False)

        yield GenerateChunk(
            delta_content="",
            is_final=True,
            usage=GenerateUsage(
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
            ),
        )

    async def shutdown(self) -> None:
        self._ready = False
        logger.info("Mock backend shut down")
