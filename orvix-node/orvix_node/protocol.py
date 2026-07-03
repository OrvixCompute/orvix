"""Wire protocol between node and orchestrator.

IMPORTANT: this file is intended to be IDENTICAL on both the node and the
orchestrator (orchestrator copy lives at app/models/protocol.py). Keep them in
sync until it is extracted into a shared package.

All messages carry {type, id, timestamp}. Parsing uses a discriminated union on
`type`, so `parse_message()` returns the correct concrete class.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, Field, TypeAdapter


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


# --- Shared GPU value objects ----------------------------------------------
class GPUInfo(BaseModel):
    vendor: str = "nvidia"
    model: str
    vram_total_mb: int
    cuda_version: Optional[str] = None
    driver_version: Optional[str] = None
    compute_capability: Optional[str] = None
    pci_bus_id: Optional[str] = None


class GPUMetrics(BaseModel):
    gpu_util_pct: int = 0
    memory_used_mb: int = 0
    memory_total_mb: int = 0
    memory_util_pct: int = 0
    temperature_c: Optional[int] = None
    power_draw_w: Optional[int] = None
    timestamp: datetime = Field(default_factory=_now)


# --- Base ------------------------------------------------------------------
class BaseMessage(BaseModel):
    id: str = Field(default_factory=_new_id)
    timestamp: datetime = Field(default_factory=_now)


# --- Outbound: node -> orchestrator ----------------------------------------
class RegisterMessage(BaseMessage):
    type: Literal["register"] = "register"
    provider_id: str
    node_secret: str
    version: str
    gpu_info: GPUInfo
    models_supported: List[str]
    max_concurrent_jobs: int
    # Engine capabilities (e.g. ["chat", "image"]) and total VRAM. Optional for
    # backward compatibility: older nodes omit them.
    engines: List[str] = Field(default_factory=list)
    vram_gb: float = 0.0


class HeartbeatMessage(BaseMessage):
    type: Literal["heartbeat"] = "heartbeat"
    status: Literal["ready", "busy", "draining"]
    current_jobs: int
    gpu_metrics: GPUMetrics


class JobResultMessage(BaseMessage):
    type: Literal["job_result"] = "job_result"
    job_id: str
    status: Literal["completed", "failed"]
    result: Optional[dict] = None  # OpenAI-format response
    error: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


class JobChunkMessage(BaseMessage):
    type: Literal["job_chunk"] = "job_chunk"
    job_id: str
    chunk: dict  # OpenAI SSE chunk
    is_final: bool = False


class ImageJobCompleteMessage(BaseMessage):
    type: Literal["job.image.complete"] = "job.image.complete"
    job_id: str
    image_id: str
    binary_url: str  # where the orchestrator fetches the PNG bytes
    metadata: dict = Field(default_factory=dict)


class ImageJobFailedMessage(BaseMessage):
    type: Literal["job.image.failed"] = "job.image.failed"
    job_id: str
    error: str


# --- Inbound: orchestrator -> node -----------------------------------------
class RegisterAckMessage(BaseMessage):
    type: Literal["register_ack"] = "register_ack"
    node_id: str
    accepted: bool
    reason: Optional[str] = None


class JobMessage(BaseMessage):
    type: Literal["job"] = "job"
    job_id: str
    model: str
    messages: List[dict]
    max_tokens: int = 512
    temperature: float = 0.7
    stream: bool = False
    user_tier: str = "bronze"


class ImageJobDispatchMessage(BaseMessage):
    type: Literal["job.image.dispatch"] = "job.image.dispatch"
    job_id: str
    model: str
    prompt: str
    width: int = 1024
    height: int = 1024
    num_inference_steps: int = 4
    seed: Optional[int] = None
    # Per-job token the node echoes back as the X-Node-Secret header on the
    # binary fetch, so the binary endpoint is authorized without sharing the
    # long-lived node secret.
    binary_token: str


class PingMessage(BaseMessage):
    type: Literal["ping"] = "ping"


class ShutdownMessage(BaseMessage):
    type: Literal["shutdown"] = "shutdown"
    reason: str = ""


# --- Unions & parsing ------------------------------------------------------
AnyMessage = Annotated[
    Union[
        RegisterMessage,
        HeartbeatMessage,
        JobResultMessage,
        JobChunkMessage,
        ImageJobCompleteMessage,
        ImageJobFailedMessage,
        RegisterAckMessage,
        JobMessage,
        ImageJobDispatchMessage,
        PingMessage,
        ShutdownMessage,
    ],
    Field(discriminator="type"),
]

_adapter: TypeAdapter = TypeAdapter(AnyMessage)


def parse_message(raw: str | bytes | dict) -> BaseMessage:
    """Parse a raw frame (JSON string/bytes or dict) into a concrete message."""
    if isinstance(raw, (str, bytes, bytearray)):
        return _adapter.validate_json(raw)
    return _adapter.validate_python(raw)


def serialize(msg: BaseMessage) -> str:
    """Serialize a message to a JSON string for sending over the wire."""
    return msg.model_dump_json()
