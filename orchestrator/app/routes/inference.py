"""OpenAI-compatible chat completions endpoint.

Routing:
  - If a ready node supports the model, the job is dispatched to it (real tokens,
    real billing, provider earns a share, jobs.is_mock = False).
  - If no node is available, we fall back to the in-process mock so development
    can continue (jobs.is_mock = True).
"""

import asyncio
import json
import time
import uuid
from collections import defaultdict, deque
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from supabase import Client

from app.config import settings
from app.database import get_supabase
from app.dependencies import get_user_from_api_key
from app.exceptions import InsufficientBalanceError, OrvixException, RateLimitError
from app.logger import logger
from app.models.inference import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)
from app.models.protocol import JobMessage
from app.services import inference_service
from app.services.billing_service import BillingService
from app.services.node_manager import NodeTimeoutError, node_manager

router = APIRouter(prefix="/v1", tags=["inference"])

# --- Simple in-memory rate limiter (per API key) ---------------------------
# TODO: replace with Redis so limits hold across processes/restarts.
RATE_LIMIT = 60  # requests
RATE_WINDOW = 60.0  # seconds
_hits: dict[str, deque] = defaultdict(deque)


def _check_rate_limit(api_key_id: str) -> None:
    now = time.monotonic()
    q = _hits[api_key_id]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise RateLimitError(
            f"Rate limit exceeded: max {RATE_LIMIT} requests per minute",
            details={"retry_after_seconds": int(RATE_WINDOW - (now - q[0])) + 1},
        )
    q.append(now)


def _provider_earning(cost: Decimal) -> Decimal:
    pct = Decimal(settings.PROVIDER_REWARD_PERCENTAGE) / Decimal(100)
    return inference_service.quantize_usdc(cost * pct)


def _record_job(
    db: Client,
    *,
    user_id: str,
    api_key_id: str,
    node_id: str | None,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost: Decimal,
    latency_ms: int,
    is_mock: bool,
    status: str = "completed",
    error_message: str | None = None,
) -> None:
    db.table("jobs").insert(
        {
            "user_id": user_id,
            "api_key_id": api_key_id,
            "node_id": node_id,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usdc": float(cost),
            "provider_earning_usdc": float(_provider_earning(cost)),
            "latency_ms": latency_ms,
            "status": status,
            "error_message": error_message,
            "is_mock": is_mock,
        }
    ).execute()


@router.post("/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    auth: dict = Depends(get_user_from_api_key),
    db: Client = Depends(get_supabase),
):
    started = time.perf_counter()
    user = auth["user"]
    api_key = auth["api_key"]
    tier = user["tier"]

    _check_rate_limit(api_key["id"])
    inference_service.validate_model(body.model)

    prompt_tokens = inference_service.estimate_prompt_tokens(body.messages)

    # Pre-generation balance check against the worst-case cost.
    billing = BillingService(db)
    max_cost = inference_service.estimate_max_cost(
        body.model, prompt_tokens, body.max_tokens, tier
    )
    current_balance = Decimal(billing.get_balance(user["id"])["balance_usdc"])
    if current_balance < max_cost:
        raise InsufficientBalanceError(
            "Insufficient USDC balance for this request",
            details={
                "current_balance": str(current_balance),
                "estimated_cost": str(max_cost),
            },
        )

    node = node_manager.select_node(body.model, tier)
    if node is None:
        logger.warning("No nodes available for {} — falling back to mock", body.model)
        return await _serve_mock(db, billing, user, api_key, body, prompt_tokens, tier, started)

    return await _serve_node(db, billing, user, api_key, node, body, prompt_tokens, tier, started)


# ===========================================================================
# Node-backed path
# ===========================================================================
async def _serve_node(db, billing, user, api_key, node, body, prompt_tokens, tier, started):
    job = JobMessage(
        job_id=str(uuid.uuid4()),
        model=body.model,
        messages=[m.model_dump() for m in body.messages],
        max_tokens=body.max_tokens,
        temperature=body.temperature,
        stream=body.stream,
        user_tier=tier,
    )

    if body.stream:
        return await _serve_node_streaming(
            db, billing, user, api_key, node, body, job, prompt_tokens, tier, started
        )

    try:
        result = await node_manager.dispatch_job(node, job)
    except NodeTimeoutError as exc:
        raise OrvixException(
            f"Node did not respond in time: {exc}",
            error_code="node_timeout",
            status_code=504,
        ) from exc

    if result.status == "failed":
        raise OrvixException(
            f"Node failed to process the job: {result.error}",
            error_code="node_error",
            status_code=502,
        )

    prompt_tokens = result.prompt_tokens or prompt_tokens
    completion_tokens = result.completion_tokens
    cost = inference_service.calculate_cost(body.model, prompt_tokens, completion_tokens, tier)

    billing.deduct_usdc(user["id"], cost)
    await node_manager.settle_job(node, cost)
    latency_ms = int((time.perf_counter() - started) * 1000)
    _record_job(
        db,
        user_id=user["id"],
        api_key_id=api_key["id"],
        node_id=node.node_id,
        model=body.model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost=cost,
        latency_ms=latency_ms,
        is_mock=False,
    )

    payload = result.result or {}
    payload.setdefault("created", int(time.time()))
    return JSONResponse(
        content=payload,
        headers={"X-Orvix-Tier": tier, "X-Orvix-Cost": str(cost), "X-Orvix-Node": node.node_id},
    )


async def _serve_node_streaming(
    db, billing, user, api_key, node, body, job, prompt_tokens, tier, started
):
    async def event_gen():
        completion_tokens = 0
        final_prompt_tokens = prompt_tokens
        try:
            gen = await node_manager.dispatch_job(node, job)
            async for chunk_msg in gen:
                chunk = chunk_msg.chunk
                usage = chunk.get("usage")
                if usage:
                    final_prompt_tokens = usage.get("prompt_tokens", final_prompt_tokens)
                    completion_tokens = usage.get("completion_tokens", completion_tokens)
                yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"
        except NodeTimeoutError as exc:
            logger.warning("Node stream timed out: {}", exc)
            yield f'data: {json.dumps({"error": {"code": "node_timeout", "message": str(exc)}})}\n\n'
            return

        # Settle billing once the stream finishes.
        cost = inference_service.calculate_cost(
            body.model, final_prompt_tokens, completion_tokens, tier
        )
        try:
            billing.deduct_usdc(user["id"], cost)
            await node_manager.settle_job(node, cost)
            latency_ms = int((time.perf_counter() - started) * 1000)
            _record_job(
                db,
                user_id=user["id"],
                api_key_id=api_key["id"],
                node_id=node.node_id,
                model=body.model,
                prompt_tokens=final_prompt_tokens,
                completion_tokens=completion_tokens,
                cost=cost,
                latency_ms=latency_ms,
                is_mock=False,
            )
        except Exception as exc:  # noqa: BLE001 — stream already delivered
            logger.error("Post-stream billing failed: {}", exc)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"X-Orvix-Tier": tier, "X-Orvix-Node": node.node_id, "Cache-Control": "no-cache"},
    )


# ===========================================================================
# Mock fallback path (no nodes connected)
# ===========================================================================
async def _serve_mock(db, billing, user, api_key, body, prompt_tokens, tier, started):
    content, completion_tokens = inference_service.generate_mock(body.messages, body.max_tokens)
    cost = inference_service.calculate_cost(body.model, prompt_tokens, completion_tokens, tier)

    if body.stream:
        return await _serve_mock_streaming(
            db, billing, user, api_key, body, content, prompt_tokens, completion_tokens, cost, tier, started
        )

    billing.deduct_usdc(user["id"], cost)
    latency_ms = int((time.perf_counter() - started) * 1000)
    _record_job(
        db,
        user_id=user["id"],
        api_key_id=api_key["id"],
        node_id=None,
        model=body.model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost=cost,
        latency_ms=latency_ms,
        is_mock=True,
    )
    completion = ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4()}",
        created=int(time.time()),
        model=body.model,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=content),
                finish_reason="stop",
            )
        ],
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )
    return JSONResponse(
        content=completion.model_dump(),
        headers={"X-Orvix-Tier": tier, "X-Orvix-Cost": str(cost), "X-Orvix-Node": "mock"},
    )


async def _serve_mock_streaming(
    db, billing, user, api_key, body, content, prompt_tokens, completion_tokens, cost, tier, started
):
    billing.deduct_usdc(user["id"], cost)
    completion_id = f"chatcmpl-{uuid.uuid4()}"
    created = int(time.time())

    async def event_gen():
        first = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": body.model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(first)}\n\n"
        for piece in inference_service.stream_mock_chunks(content):
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": body.model,
                "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            await asyncio.sleep(0.05)
        final = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": body.model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(final)}\n\n"
        yield "data: [DONE]\n\n"
        latency_ms = int((time.perf_counter() - started) * 1000)
        _record_job(
            db,
            user_id=user["id"],
            api_key_id=api_key["id"],
            node_id=None,
            model=body.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            latency_ms=latency_ms,
            is_mock=True,
        )

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"X-Orvix-Tier": tier, "X-Orvix-Cost": str(cost), "X-Orvix-Node": "mock"},
    )
