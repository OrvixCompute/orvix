"""Pydantic model matching the OpenAI image-generation request shape."""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class ImageGenerationRequest(BaseModel):
    model: str = "flux-schnell"
    prompt: str = Field(..., min_length=1)
    n: int = Field(1, ge=1, le=4)
    size: str = "1024x1024"
    response_format: Literal["url", "b64_json"] = "url"
    user: Optional[str] = None
