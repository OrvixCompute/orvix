"""GET /v1/models — public catalog of chat + image models (OpenAI-compatible)."""

import time

from fastapi import APIRouter

from app.models.inference import MODEL_CATALOG

router = APIRouter(prefix="/v1", tags=["models"])


@router.get("/models")
async def list_models() -> dict:
    created = int(time.time())
    data = [
        {
            "id": entry["id"],
            "object": "model",
            "created": created,
            "owned_by": "orvix",
            # Orvix-specific hints (extra fields are ignored by OpenAI clients).
            "type": entry["type"],
            **{k: v for k, v in entry.items() if k not in ("id", "type")},
        }
        for entry in MODEL_CATALOG
    ]
    return {"object": "list", "data": data}
