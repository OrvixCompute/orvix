"""OpenAI-shaped video generation endpoint: POST /v1/videos/generations.

Flow mirrors the image path: authenticate → validate resolution against the
catalog → storage cap → daily quota gate → rate limit (free flow) → pick a
video-capable node → dispatch a video job over the WebSocket → wait for
completion → fetch the MP4 bytes from the node's binary endpoint → save
locally → record the job → return the URL.

**Free + daily quota during the alpha.** A clip costs minutes of GPU on the
node, so the daily allowance is the limiter; USDC billing lands with the rest
of the priced engines. ``n`` is fixed at 1 for the same reason: batching is the
wrong optimization for a resource that serializes the card for minutes.

Deploy note — nginx must serve VIDEO_STORAGE_DIR at PUBLIC_VIDEO_URL_BASE. Add
(apply manually on the VPS):

    location /videos/ {
        alias /var/orvix/videos/;
        add_header Cache-Control "public, max-age=86400";
        try_files $uri =404;
    }
"""

from __future__ import annotations

import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from supabase import Client

from app.config import settings
from app.database import get_supabase
from app.dependencies import get_user_from_api_key
from app.exceptions import OrvixException, ValidationError
from app.logger import logger
from app.models.inference import MODEL_CATALOG, VIDEO_MODELS
from app.models.protocol import VideoJobDispatchMessage
from app.models.video import VideoGenerationRequest
from app.services import (
    quota_service,
    rate_limit_service,
    storage_service,
    tier_service,
)
from app.services.billing_service import BillingService
from app.services.holder import holder_service
from app.services.node_manager import (
    CAPACITY_RETRY_AFTER_SECONDS,
    NodeTimeoutError,
    node_manager,
)

router = APIRouter(prefix="/v1", tags=["videos"])


def _validate_resolution(model: str, width: int, height: int) -> None:
    """Reject a resolution the model cannot produce (catalog max_size).

    The request model already bounds width/height (256-1280 / 256-720); this
    narrows further per model so a request that would only fail on the node is
    refused before quota is consumed.
    """
    max_size = _max_size_for(model)
    max_w, max_h = (int(p) for p in max_size.split("x"))
    if width > max_w or height > max_h:
        raise ValidationError(
            f"Resolution {width}x{height} exceeds the maximum {max_w}x{max_h} "
            f"for model '{model}'.",
            error_code="invalid_size",
        )
    # LTX-Video (the orvix-video-1 backend) requires both dimensions to be
    # divisible by 32. Without this check the request passes validation, burns
    # quota, waits for the node to load the model, then fails with a ValueError
    # on the node — the exact failure mode this function exists to prevent.
    if width % 32 != 0 or height % 32 != 0:
        raise ValidationError(
            f"Resolution {width}x{height} must be a multiple of 32 for model "
            f"'{model}' (e.g. 640x384 or 1280x704).",
            error_code="invalid_size",
        )


def _max_size_for(model: str) -> str:
    for entry in MODEL_CATALOG:
        if entry["id"] == model and entry["type"] == "video":
            return entry["max_size"]
    raise KeyError(f"{model!r} is not a video model in the catalog")


async def _fetch_video_bytes(binary_url: str, token: str) -> bytes:
    """Fetch the generated MP4 from the node's binary endpoint (X-Node-Secret auth)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(binary_url, headers={"X-Node-Secret": token})
        r.raise_for_status()
        return r.content


def _save_video(mp4_bytes: bytes) -> tuple[str, str]:
    """Write bytes to VIDEO_STORAGE_DIR/<uuid>.mp4. Return (filename, public_url)."""
    os.makedirs(settings.VIDEO_STORAGE_DIR, exist_ok=True)
    filename = f"{uuid.uuid4()}.mp4"
    path = os.path.join(settings.VIDEO_STORAGE_DIR, filename)
    with open(path, "wb") as f:
        f.write(mp4_bytes)
    public_url = f"{settings.PUBLIC_VIDEO_URL_BASE.rstrip('/')}/{filename}"
    return filename, public_url


@router.post("/videos/generations")
async def videos_generations(
    body: VideoGenerationRequest,
    auth: dict = Depends(get_user_from_api_key),
    db: Client = Depends(get_supabase),
):
    user = auth["user"]
    api_key = auth["api_key"]
    tier = tier_service.tier_for_stake(user.get("staked_orvx"))

    if body.model not in VIDEO_MODELS:
        raise ValidationError(
            f"Model '{body.model}' is not a video model. Choose one of: "
            f"{', '.join(VIDEO_MODELS)}",
            error_code="model_not_found",
        )
    _validate_resolution(body.model, body.width, body.height)

    # Storage safety cap — refuse before consuming quota when the disk is full.
    # Video files are ~5-10 MB each, so the cap is a hard ceiling on how much
    # of the disk a burst of clips can claim before the 24h cleanup runs.
    if storage_service.compute_size_mb(settings.VIDEO_STORAGE_DIR) > settings.MAX_VIDEO_STORAGE_MB:
        raise OrvixException(
            "Video storage is temporarily full. Cleanup in progress.",
            error_code="storage_full",
            status_code=503,
        )

    # Quota gate: same shape as images — holders get VIDEO_DAILY_LIMIT_HOLDER/day,
    # everyone gets the fallback when ORVX_MINT_ADDRESS is unset. Free during the
    # alpha, so the paid flow is unreachable in practice; enforce_video_quota
    # still knows about it so a future priced phase changes nothing here.
    is_holder, balance = await holder_service.get_holder_status(db, user["wallet_address"])
    billing = BillingService(db)
    current_balance = Decimal(billing.get_balance(user["id"])["balance_usdc"])
    quota = quota_service.enforce_video_quota(
        db, user["wallet_address"], is_holder, balance, units=1, usdc_balance=current_balance
    )
    free = quota["free"]

    if free:
        rate_limit_service.check(api_key["id"], tier, bucket="video")

    node = node_manager.select_video_node(body.model)
    if node is None:
        if free:
            quota_service.refund_video_quota(db, user["wallet_address"], 1)
        reason = node_manager.unavailable_reason(body.model, engine="video")
        if reason == "at_capacity":
            raise OrvixException(
                "All video providers serving this model are busy. Retry shortly.",
                error_code="capacity_exhausted",
                status_code=503,
                details={"retry_after_seconds": CAPACITY_RETRY_AFTER_SECONDS},
            )
        raise OrvixException(
            "No video providers are currently available",
            error_code="no_video_provider",
            status_code=503,
        )

    job_id = str(uuid.uuid4())
    binary_token = secrets.token_urlsafe(32)
    dispatch = VideoJobDispatchMessage(
        job_id=job_id,
        model=body.model,
        prompt=body.prompt,
        negative_prompt=body.negative_prompt,
        width=body.width,
        height=body.height,
        num_frames=body.num_frames,
        fps=body.fps,
        num_inference_steps=body.num_inference_steps,
        guidance_scale=body.guidance_scale,
        seed=body.seed,
        binary_token=binary_token,
    )

    created = int(time.time())
    try:
        complete = await node_manager.dispatch_video_job(node, dispatch)
    except NodeTimeoutError as exc:
        if free:
            quota_service.refund_video_quota(db, user["wallet_address"], 1)
        raise OrvixException(
            f"Video node did not respond in time: {exc}",
            error_code="node_timeout",
            status_code=504,
        ) from exc
    except RuntimeError as exc:
        if free:
            quota_service.refund_video_quota(db, user["wallet_address"], 1)
        raise OrvixException(
            f"Video node failed to generate: {exc}",
            error_code="node_error",
            status_code=502,
        ) from exc

    try:
        mp4_bytes = await _fetch_video_bytes(complete.binary_url, binary_token)
    except httpx.HTTPError as exc:
        if free:
            quota_service.refund_video_quota(db, user["wallet_address"], 1)
        raise OrvixException(
            f"Failed to fetch video from node: {exc}",
            error_code="node_error",
            status_code=502,
        ) from exc

    _filename, public_url = _save_video(mp4_bytes)

    # Not billed during the alpha (cost 0), so there is no per-job settlement.
    _record_video_job(
        db,
        cost=Decimal("0"),
        user_id=user["id"],
        provider_id=node.provider_id,
        model=body.model,
        prompt=body.prompt,
        width=body.width,
        height=body.height,
        num_frames=body.num_frames,
        fps=body.fps,
        video_url=public_url,
    )

    return JSONResponse(
        content={"created": created, "data": [{"url": public_url}]},
        headers={
            "X-Orvix-Quota-Remaining": str(quota["remaining"]),
            "X-Orvix-Quota-Reset": quota["reset_at"],
        },
    )


def _record_video_job(
    db: Client,
    *,
    cost: Decimal,
    user_id: str,
    provider_id: str,
    model: str,
    prompt: str,
    width: int,
    height: int,
    num_frames: int,
    fps: int,
    video_url: str,
) -> None:
    now = datetime.now(timezone.utc)
    try:
        db.table("video_jobs").insert(
            {
                "user_id": user_id,
                "provider_id": provider_id,
                "model": model,
                "prompt": prompt[:500],
                "width": width,
                "height": height,
                "num_frames": num_frames,
                "fps": fps,
                "cost_usdc": float(cost),
                "video_url": video_url,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=24)).isoformat(),
            }
        ).execute()
    except Exception as exc:  # noqa: BLE001 — video already generated; don't fail the request
        logger.error("Failed to record video_job: {}", exc)
