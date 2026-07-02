"""Abstract inference interfaces + the request/response value objects.

The node runs one or more *engines*. Every engine — chat or image — shares a
common lifecycle (:class:`AbstractEngine`: load / unload / is_loaded) so a future
ModelManager (Session 2) can swap them in and out of VRAM uniformly.

On top of that lifecycle sit two families:

* :class:`ChatEngine` — text generation. It keeps the original
  ``InferenceBackend`` contract (initialize / is_ready / generate /
  generate_stream / shutdown) so existing chat behavior is unchanged; the engine
  lifecycle is bridged onto it.
* :class:`ImageEngine` — image generation via :meth:`ImageEngine.infer`.

Swapping chat backends (mock -> vLLM) is still a one-file change because the
executor only ever talks to the ``InferenceBackend`` contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, ClassVar, List, Optional, Protocol, runtime_checkable

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


# --- Chat backend contract (unchanged) -------------------------------------
@runtime_checkable
class InferenceBackend(Protocol):
    async def initialize(self, model: str) -> None: ...

    async def is_ready(self) -> bool: ...

    async def generate(self, request: GenerateRequest) -> GenerateResponse: ...

    def generate_stream(self, request: GenerateRequest) -> AsyncIterator[GenerateChunk]: ...

    async def shutdown(self) -> None: ...


# --- Engine hierarchy ------------------------------------------------------
class AbstractEngine(ABC):
    """Common lifecycle + capability metadata for every inference engine.

    ``engine_type`` ("chat" | "image"), ``required_vram_gb`` and
    ``supported_models`` describe the engine so the ModelManager (Session 2) and
    the node's capability advertisement can reason about it without loading it.
    """

    engine_type: ClassVar[str] = ""
    required_vram_gb: ClassVar[float] = 0.0
    supported_models: ClassVar[List[str]] = []

    @abstractmethod
    async def load(self) -> None:
        """Bring the model into VRAM. Idempotent: a no-op if already loaded."""

    @abstractmethod
    async def unload(self) -> None:
        """Free VRAM. Idempotent: a no-op if not loaded."""

    @abstractmethod
    async def is_loaded(self) -> bool:
        """True when the model is resident and ready to serve."""


class ChatEngine(AbstractEngine):
    """Base for chat/text engines.

    Concrete backends implement the original ``InferenceBackend`` contract; this
    base bridges the engine lifecycle (load/unload/is_loaded) onto it so chat
    behavior is identical to before while the ModelManager gets a uniform API.
    """

    engine_type: ClassVar[str] = "chat"

    # Set by concrete backends (VLLMBackend sets it in __init__, MockBackend too).
    model: Optional[str] = None

    # --- chat contract (implemented by concrete backends) ---
    @abstractmethod
    async def initialize(self, model: str) -> None: ...

    @abstractmethod
    async def is_ready(self) -> bool: ...

    @abstractmethod
    async def generate(self, request: GenerateRequest) -> GenerateResponse: ...

    @abstractmethod
    def generate_stream(
        self, request: GenerateRequest
    ) -> AsyncIterator[GenerateChunk]: ...

    @abstractmethod
    async def shutdown(self) -> None: ...

    # --- engine lifecycle (delegates to the chat contract) ---
    async def load(self) -> None:
        await self.initialize(self.model or "")

    async def unload(self) -> None:
        await self.shutdown()

    async def is_loaded(self) -> bool:
        return await self.is_ready()


class ImageEngine(AbstractEngine):
    """Base for image engines. Concrete engines implement :meth:`infer`."""

    engine_type: ClassVar[str] = "image"

    @abstractmethod
    async def infer(self, request: ImageRequest) -> ImageResult:
        """Generate one image and return its PNG bytes + metadata."""
