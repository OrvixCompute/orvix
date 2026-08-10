"""Abstract inference interfaces + the request/response value objects.

The node runs one or more *engines*. Every engine — chat or image — shares a
common lifecycle (:class:`AbstractEngine`: ``load(model_id)`` / ``unload`` /
``is_loaded``) so the :class:`~orvix_node.inference.manager.ModelManager` can
swap them in and out of VRAM uniformly.

On top of that lifecycle sit two families:

* :class:`ChatEngine` — text generation (``generate`` / ``generate_stream``).
* :class:`ImageEngine` — image generation (``infer``).
* :class:`VideoEngine` — text-to-video generation (``infer``).

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
    # OpenAI-shaped tool definitions, passed straight through to the engine.
    tools: Optional[List[dict]] = None
    tool_choice: Optional[object] = None


class GenerateUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int


class GenerateResponse(BaseModel):
    # Empty when the model answered purely with tool calls.
    content: str = ""
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str = "stop"  # "stop" | "length" | "tool_calls"
    # Raw OpenAI-shaped tool calls from the engine, forwarded as-is.
    tool_calls: Optional[List[dict]] = None


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


# --- Video value objects ---------------------------------------------------
class VideoRequest(BaseModel):
    """A single text-to-video request.

    Bounds are deliberately tighter than the image ones. A clip costs roughly
    ``num_frames`` times an image, so an unbounded request is not merely a slow
    request but a wedged GPU: the node holds the card for the whole generation
    and every other job queues behind it.
    """

    prompt: str
    negative_prompt: Optional[str] = None
    width: int = Field(704, ge=256, le=1280)
    height: int = Field(480, ge=256, le=720)
    # Latent video pipelines expect 8k+1 frames, so the default is a valid
    # count rather than a round one. See VideoEngine.normalize_frames.
    num_frames: int = Field(97, ge=9, le=257)
    fps: int = Field(24, ge=8, le=60)
    num_inference_steps: int = Field(30, ge=1, le=60)
    guidance_scale: float = 3.0
    seed: Optional[int] = None


class VideoResult(BaseModel):
    """Encoded MP4 bytes plus generation metadata.

    Carried as bytes for the same reason :class:`ImageResult` is — this module
    must stay importable without torch, diffusers, or a GPU.
    """

    mp4_bytes: bytes
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


class VideoEngine(AbstractEngine):
    """Base for text-to-video engines. Concrete engines implement :meth:`infer`.

    A sibling of :class:`ImageEngine` rather than a subclass: the two share only
    the lifecycle, and a clip is a different enough unit of work — minutes
    instead of seconds, an MP4 instead of a PNG — that inheriting image's shape
    would mislead every caller that switches on ``engine_type``.
    """

    engine_type: ClassVar[str] = "video"

    @staticmethod
    def normalize_frames(num_frames: int) -> int:
        """Round ``num_frames`` up to the nearest 8k+1.

        Latent video pipelines compress time by 8, so a count that is not 8k+1
        is silently altered by the pipeline — the caller asks for 100 frames,
        gets 97, and the duration implied by their fps is wrong. Rounding here
        keeps the returned metadata honest about what was produced.
        """
        if num_frames < 9:
            return 9
        return ((num_frames - 1 + 7) // 8) * 8 + 1

    @abstractmethod
    async def infer(self, request: VideoRequest) -> VideoResult:
        """Generate one clip and return its MP4 bytes + metadata."""
