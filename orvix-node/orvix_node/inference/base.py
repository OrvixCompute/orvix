"""Abstract inference interfaces + the request/response value objects.

The node runs one or more *engines*. Every engine — chat or image — shares a
common lifecycle (:class:`AbstractEngine`: ``load(model_id)`` / ``unload`` /
``is_loaded``) so the :class:`~orvix_node.inference.manager.ModelManager` can
swap them in and out of VRAM uniformly.

On top of that lifecycle sit two families:

* :class:`ChatEngine` — text generation (``generate`` / ``generate_stream``).
* :class:`ImageEngine` — image generation (``infer``).

``load`` takes the orchestrator-facing ``model_id`` so a single engine can serve
several models later; engines that serve a fixed model may ignore the argument.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, ClassVar, List, Optional

from pydantic import BaseModel, Field


# --- Chat value objects ----------------------------------------------------
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


# --- Image value objects ---------------------------------------------------
class ImageRequest(BaseModel):
    """A single image-generation request. Bounds mirror what the orchestrator
    exposes; Flux Schnell needs only 1–4 steps and no guidance."""

    prompt: str
    width: int = Field(1024, ge=256, le=1536)
    height: int = Field(1024, ge=256, le=1536)
    num_inference_steps: int = Field(4, ge=1, le=8)
    seed: Optional[int] = None
    guidance_scale: float = 0.0


class ImageResult(BaseModel):
    """Encoded PNG bytes plus generation metadata. The image is carried as bytes
    (not a PIL object) so this module stays free of heavy GPU-only imports."""

    png_bytes: bytes
    metadata: dict = Field(default_factory=dict)


# --- Engine hierarchy ------------------------------------------------------
class AbstractEngine(ABC):
    """Common lifecycle + capability metadata for every inference engine.

    ``engine_type`` ("chat" | "image"), ``required_vram_gb`` and
    ``supported_models`` describe the engine so the ModelManager and the node's
    capability advertisement can reason about it without loading it.
    """

    engine_type: ClassVar[str] = ""
    required_vram_gb: ClassVar[float] = 0.0
    supported_models: ClassVar[List[str]] = []

    @abstractmethod
    async def load(self, model_id: str) -> None:
        """Bring ``model_id`` into VRAM. Idempotent: a no-op if already loaded."""

    @abstractmethod
    async def unload(self) -> None:
        """Free VRAM. Idempotent: a no-op if not loaded."""

    @abstractmethod
    async def is_loaded(self) -> bool:
        """True when the model is resident and ready to serve."""


class ChatEngine(AbstractEngine):
    """Base for chat/text engines. Concrete backends implement the lifecycle
    (``load`` / ``unload`` / ``is_loaded``) plus ``generate`` /
    ``generate_stream``."""

    engine_type: ClassVar[str] = "chat"

    @abstractmethod
    async def generate(self, request: GenerateRequest) -> GenerateResponse: ...

    @abstractmethod
    def generate_stream(
        self, request: GenerateRequest
    ) -> AsyncIterator[GenerateChunk]: ...


class ImageEngine(AbstractEngine):
    """Base for image engines. Concrete engines implement :meth:`infer`."""

    engine_type: ClassVar[str] = "image"

    @abstractmethod
    async def infer(self, request: ImageRequest) -> ImageResult:
        """Generate one image and return its PNG bytes + metadata."""
