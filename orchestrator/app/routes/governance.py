"""Governance endpoints. v1 governance is off-chain via Snapshot.org.

This intentionally does NOT proxy live Snapshot data (rate limits); it just
surfaces the space URL for the frontend to link out to.
"""

from fastapi import APIRouter

from app.config import settings

router = APIRouter(prefix="/v1/governance", tags=["governance"])


@router.get("/snapshot-url")
async def snapshot_url():
    return {"space": settings.GOVERNANCE_SNAPSHOT_SPACE, "url": settings.GOVERNANCE_SNAPSHOT_URL}
