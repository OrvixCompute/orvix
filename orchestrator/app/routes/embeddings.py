"""OpenAI-compatible embeddings endpoint.

Deliberately the simplest of the three inference paths. An embedding answer is
one small JSON payload with no streaming, no binary transport and no storage, so
this route reuses the chat path's blocking dispatch rather than inventing a
third completion mechanism to keep in step with the other two.

**Not billed.** Embeddings are free during the alpha, rate-limited per API key in
their own bucket. That is a stated policy, not an oversight: there is no pricing
unit for embeddings yet, and charging against a unit nobody has agreed on would
be worse than charging nothing. Wiring billing later means adding a quota gate
here, exactly as chat and images already have.
"""

import base64
import struct
import time
import uuid

from fastapi import APIRouter, Depends
from supabase import Client

from app.database import get_supabase
from app.dependencies import get_user_from_api_key
from app.exceptions import OrvixException, ValidationError
from app.logger import logger
from app.models.embedding import (
    EmbeddingObject,
    EmbeddingRequest,
    EmbeddingResponse,
    EmbeddingUsage,
)
from app.models.inference import MODEL_CATALOG
from app.models.protocol import EmbeddingJobDispatchMessage
from app.services import rate_limit_service, tier_service
from app.services.node_manager import NodeTimeoutError, node_manager

router = APIRouter(prefix="/v1", tags=["embeddings"])

EMBEDDING_MODELS = [m["id"] for m in MODEL_CATALOG if m["type"] == "embedding"]
CAPACITY_RETRY_AFTER_SECONDS = 3


def _estimate_tokens(texts: list[str]) -> int:
    """Rough token count for usage reporting.

    The same ~4-chars-per-token heuristic the chat path uses. Usage is reported
    because OpenAI clients read it; it is not used to bill anything here, so an
    approximation is honest rather than convenient.
    """
    return max(1, sum(len(t) for t in texts) // 4)


def _to_base64(vector: list[float]) -> str:
    """Little-endian float32 packing — the layout OpenAI's base64 format uses."""
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


@router.post("/embeddings", response_model=EmbeddingResponse)
async def create_embeddings(
    body: EmbeddingRequest,
    auth: dict = Depends(get_user_from_api_key),
    db: Client = Depends(get_supabase),
):
    user = auth["user"]
    api_key = auth["api_key"]
    tier = tier_service.tier_for_stake(user.get("staked_orvx"))

    if body.model not in EMBEDDING_MODELS:
        raise ValidationError(
            f"Model '{body.model}' is not an embedding model. Choose one of: "
            f"{', '.join(EMBEDDING_MODELS)}",
            error_code="model_not_found",
        )

    # Own bucket, so an embedding burst cannot spend the caller's chat allowance
    # — a RAG job indexing documents would otherwise lock them out of chat.
    rate_limit_service.check(api_key["id"], tier, bucket="embedding")

    texts = body.as_list()

    node = node_manager.select_embedding_node(body.model)
    if node is None:
        # Same split the other two paths make: busy is transient and worth a
        # retry, absent is not. Collapsing them tells a caller to retry forever
        # against a network that has nobody serving the model.
        busy = any(
            c.status == "ready" and "embedding" in c.engines and body.model in c.models_supported
            for c in node_manager.connected_nodes.values()
        )
        logger.warning(
            "No embedding node for {} (busy={}) — refusing", body.model, busy
        )
        if busy:
            raise OrvixException(
                "All compute providers serving this model are busy. Retry shortly.",
                error_code="capacity_exhausted",
                status_code=503,
                details={"retry_after_seconds": CAPACITY_RETRY_AFTER_SECONDS},
            )
        raise OrvixException(
            "No compute providers are currently serving embeddings",
            error_code="no_embedding_provider",
            status_code=503,
        )

    started = time.perf_counter()
    job = EmbeddingJobDispatchMessage(
        job_id=str(uuid.uuid4()), model=body.model, input=texts
    )
    try:
        result = await node_manager.dispatch_embedding_job(node, job)
    except NodeTimeoutError as exc:
        raise OrvixException(
            f"Node did not respond in time: {exc}",
            error_code="node_timeout",
            status_code=504,
        ) from exc

    if result.status != "completed":
        raise OrvixException(
            result.error or "Embedding generation failed",
            error_code="inference_failed",
            status_code=502,
        )

    vectors = (result.result or {}).get("embeddings")
    # Guard the shape before handing it to a client. A node that returns the
    # wrong number of vectors would otherwise misalign every embedding with its
    # input — silent, and ruinous for a vector store.
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        logger.error(
            "Node {} returned {} vectors for {} inputs",
            node.node_id,
            len(vectors) if isinstance(vectors, list) else "non-list",
            len(texts),
        )
        raise OrvixException(
            "Node returned a malformed embedding response",
            error_code="inference_failed",
            status_code=502,
        )

    data = [
        EmbeddingObject(
            index=i,
            embedding=_to_base64(vec) if body.encoding_format == "base64" else vec,
        )
        for i, vec in enumerate(vectors)
    ]
    tokens = _estimate_tokens(texts)
    logger.info(
        "Embedded {} input(s) on node {} in {:.0f} ms",
        len(texts),
        node.node_id,
        (time.perf_counter() - started) * 1000,
    )
    return EmbeddingResponse(
        data=data,
        model=body.model,
        usage=EmbeddingUsage(prompt_tokens=tokens, total_tokens=tokens),
    )
