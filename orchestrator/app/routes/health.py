"""Health and API-info endpoints."""

from fastapi import APIRouter

from app import __version__
from app.database import test_connection

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Liveness + DB connectivity check."""
    db_ok = test_connection()
    return {
        "status": "ok",
        "version": __version__,
        "db": "connected" if db_ok else "error",
    }


@router.get("/v1")
async def api_info() -> dict:
    """Basic API metadata."""
    return {
        "name": "Orvix Orchestrator",
        "version": __version__,
        "description": "Decentralized AI compute network on Solana — OpenAI-compatible API.",
        "endpoints": {
            "auth": "/v1/auth",
            "api_keys": "/v1/api-keys",
            "chat_completions": "/v1/chat/completions",
            "billing": "/v1/billing",
        },
    }
