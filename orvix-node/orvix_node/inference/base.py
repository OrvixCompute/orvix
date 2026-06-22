"""Abstract inference backend interface + the request/response value objects.

Swapping backends (mock -> vLLM) is a one-file change because the executor only
ever talks to this interface.
"""

from __future__ import annotations

from typing import AsyncIterator, List, Optional, Protocol, runtime_checkable

from pydantic import BaseModel


class GenerateRequest(BaseModel):
    messages: List[dict]
    max_tokens: int = 512
    temperature: float = 0.7


class GenerateUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int


class GenerateResponse(BaseModel):
    content: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str = "stop"  # "stop" | "length"


class GenerateChunk(BaseModel):
    delta_content: str = ""
    is_final: bool = False
    usage: Optional[GenerateUsage] = None  # populated only on the final chunk


@runtime_checkable
class InferenceBackend(Protocol):
    async def initialize(self, model: str) -> None: ...

    async def is_ready(self) -> bool: ...

    async def generate(self, request: GenerateRequest) -> GenerateResponse: ...

    def generate_stream(self, request: GenerateRequest) -> AsyncIterator[GenerateChunk]: ...

    async def shutdown(self) -> None: ...
