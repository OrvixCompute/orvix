"""Pydantic response models for the public network stats endpoint."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class NodeStats(BaseModel):
    registered: int = Field(..., description="Nodes that have ever registered")
    online: int = Field(..., description="Nodes holding a live websocket connection right now")
    ready: int
    busy: int
    draining: int
    offline: int
    chat_capable: int
    image_capable: int
    total_vram_gb: str


class GpuBreakdown(BaseModel):
    gpu_model: str
    count: int


class ProviderStats(BaseModel):
    total: int = Field(..., description="Accounts flagged as providers")
    staked: int = Field(..., description="Providers with a non-zero ORVX stake")


class ChatStats(BaseModel):
    requests_total: int
    requests_window: int
    tokens_total: int
    tokens_window: int
    avg_latency_ms: Optional[int] = Field(
        None, description="Mean latency over the window; null when there were no requests"
    )


class ImageStats(BaseModel):
    generated_total: int
    generated_window: int


class ModelStats(BaseModel):
    chat: int = Field(..., description="Chat models in the catalog")
    image: int = Field(..., description="Image models in the catalog")
    chat_available: int = Field(
        ..., description="Chat models a currently connected node actually serves"
    )
    image_available: int = Field(
        ..., description="Image models a currently connected node actually serves"
    )


class NetworkStatsResponse(BaseModel):
    window_hours: int = Field(..., description="Rolling window the *_window counters cover")
    nodes: NodeStats
    gpus: list[GpuBreakdown]
    providers: ProviderStats
    chat: ChatStats
    images: ImageStats
    models: ModelStats
    generated_at: datetime = Field(..., description="When these numbers were computed (cache stamp)")
