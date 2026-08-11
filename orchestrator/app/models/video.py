"""Pydantic model matching the video-generation request shape.

Defaults and bounds mirror the node's VideoRequest (orvix-node
inference/base.py) so a bare request matches the engine's intent: width
256-1280, height 256-720, num_frames 9-257, fps 8-60, num_inference_steps
1-60, guidance_scale 0-20.
"""

from typing import Optional

from pydantic import BaseModel, Field


class VideoGenerationRequest(BaseModel):
    model: str = "orvix-video-1"
    prompt: str = Field(..., min_length=1)
    negative_prompt: Optional[str] = None
    width: int = Field(704, ge=256, le=1280)
    height: int = Field(480, ge=256, le=720)
    num_frames: int = Field(97, ge=9, le=257)
    fps: int = Field(24, ge=8, le=60)
    num_inference_steps: int = Field(30, ge=1, le=60)
    guidance_scale: float = Field(3.0, ge=0.0, le=20.0)
    seed: Optional[int] = None
    user: Optional[str] = None
