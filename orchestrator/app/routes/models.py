"""GET /v1/models — public catalog of chat + image models (OpenAI-compatible)."""

import time

from fastapi import APIRouter

from app.models.inference import MODEL_CATALOG
from app.services.node_manager import node_manager

router = APIRouter(prefix="/v1", tags=["models"])


@router.get("/models")
async def list_models() -> dict:
    created = int(time.time())
    # The catalog is static, but which of its models a node actually runs is not.
    # Listing all of them as if they were equally usable sent clients straight
    # into a 503 for a model nothing on the network serves, with no way to tell
    # beforehand which ones were real.
    served = node_manager.served_models()
    data = [
        {
            "id": entry["id"],
            "object": "model",
            "created": created,
            "owned_by": "orvix",
            # Orvix-specific hints (extra fields are ignored by OpenAI clients).
            "type": entry["type"],
            "available": entry["id"] in served,
            **{k: v for k, v in entry.items() if k not in ("id", "type")},
        }
        for entry in MODEL_CATALOG
    ]
    return {"object": "list", "data": data}
