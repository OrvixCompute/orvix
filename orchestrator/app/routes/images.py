"""OpenAI DALL-E-compatible image generation endpoint: POST /v1/images/generations.

Flow: authenticate → pick an image-capable node → dispatch an image job over the
WebSocket → wait for completion → fetch the PNG bytes from the node's binary
endpoint → save locally → record the job → return URL(s).

Deploy note — nginx must serve IMAGE_STORAGE_DIR at PUBLIC_IMAGE_URL_BASE. Add
(apply manually on the VPS):

    location /images/ {
        alias /var/orvix/images/;
        add_header Cache-Control "public, max-age=86400";
        try_files $uri =404;
    }
"""

from __future__ import annotations

import base64
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends
from supabase import Client

from app.config import settings
from app.database import get_supabase
from app.dependencies import get_user_from_api_key
from app.exceptions import OrvixException, ValidationError
from app.logger import logger
from app.models.image import ImageGenerationRequest
from app.models.inference import IMAGE_MODELS
from app.models.protocol import ImageJobDispatchMessage
from app.services.node_manager import NodeTimeoutError, node_manager

router = APIRouter(prefix="/v1", tags=["images"])

_ALLOWED_SIZES = {"256x256", "512x512", "1024x1024", "1024x1792", "1792x1024", "1536x1536"}


def _parse_size(size: str) -> tuple[int, int]:
    if size not in _ALLOWED_SIZES:
        raise ValidationError(
            f"Unsupported size '{size}'. Choose one of: {', '.join(sorted(_ALLOWED_SIZES))}",
            error_code="invalid_size",
        )
    w, h = size.lower().split("x")
    return int(w), int(h)


async def _fetch_image_bytes(binary_url: str, token: str) -> bytes:
    """Fetch the generated PNG from the node's binary endpoint (X-Node-Secret auth)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(binary_url, headers={"X-Node-Secret": token})
        r.raise_for_status()
        return r.content


def _save_image(png_bytes: bytes) -> tuple[str, str]:
    """Write bytes to IMAGE_STORAGE_DIR/<uuid>.png. Return (filename, public_url)."""
    os.makedirs(settings.IMAGE_STORAGE_DIR, exist_ok=True)
    filename = f"{uuid.uuid4()}.png"
    path = os.path.join(settings.IMAGE_STORAGE_DIR, filename)
    with open(path, "wb") as f:
        f.write(png_bytes)
    public_url = f"{settings.PUBLIC_IMAGE_URL_BASE.rstrip('/')}/{filename}"
    return filename, public_url


@router.post("/images/generations")
async def images_generations(
    body: ImageGenerationRequest,
    auth: dict = Depends(get_user_from_api_key),
    db: Client = Depends(get_supabase),
):
    user = auth["user"]

    if body.model not in IMAGE_MODELS:
        raise ValidationError(
            f"Model '{body.model}' is not an image model. Choose one of: "
            f"{', '.join(IMAGE_MODELS)}",
            error_code="model_not_found",
        )
    width, height = _parse_size(body.size)

    # TODO(Session 4): enforce the holder image quota here (5/day for holders,
    # non-holders blocked; 1/day grace when ORVX_MINT_ADDRESS is unset).

    node = node_manager.select_image_node(body.model)
    if node is None:
        raise OrvixException(
            "No image providers are currently available",
            error_code="no_image_provider",
            status_code=503,
        )

    created = int(time.time())
    data: list[dict] = []
    for _ in range(body.n):
        job_id = str(uuid.uuid4())
        binary_token = secrets.token_urlsafe(32)
        dispatch = ImageJobDispatchMessage(
            job_id=job_id,
            model=body.model,
            prompt=body.prompt,
            width=width,
            height=height,
            binary_token=binary_token,
        )
        try:
            complete = await node_manager.dispatch_image_job(node, dispatch)
        except NodeTimeoutError as exc:
            raise OrvixException(
                f"Image node did not respond in time: {exc}",
                error_code="node_timeout",
                status_code=504,
            ) from exc
        except RuntimeError as exc:
            raise OrvixException(
                f"Image node failed to generate: {exc}",
                error_code="node_error",
                status_code=502,
            ) from exc

        png_bytes = await _fetch_image_bytes(complete.binary_url, binary_token)
        _filename, public_url = _save_image(png_bytes)

        _record_image_job(
            db,
            user_id=user["id"],
            provider_id=node.provider_id,
            model=body.model,
            prompt=body.prompt,
            width=width,
            height=height,
            image_url=public_url,
        )

        if body.response_format == "b64_json":
            data.append({"b64_json": base64.b64encode(png_bytes).decode(), "revised_prompt": None})
        else:
            data.append({"url": public_url, "revised_prompt": None})

    return {"created": created, "data": data}


def _record_image_job(
    db: Client,
    *,
    user_id: str,
    provider_id: str,
    model: str,
    prompt: str,
    width: int,
    height: int,
    image_url: str,
) -> None:
    now = datetime.now(timezone.utc)
    try:
        db.table("image_jobs").insert(
            {
                "user_id": user_id,
                "provider_id": provider_id,
                "model": model,
                "prompt": prompt[:500],
                "width": width,
                "height": height,
                "cost_usdc": 0,  # TODO(Session 4): variable pricing for non-holders
                "image_url": image_url,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=24)).isoformat(),
            }
        ).execute()
    except Exception as exc:  # noqa: BLE001 — image already generated; don't fail the request
        logger.error("Failed to record image_job: {}", exc)
