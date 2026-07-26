"""Public network stats.

Public (no auth):
    GET /v1/network/stats   node/GPU capacity, request + token volume, model counts

Aggregates are cached (see NETWORK_STATS_CACHE_SECONDS) and exclude mock jobs,
so the numbers reflect compute that real nodes actually served.
"""

from fastapi import APIRouter, Depends
from supabase import Client

from app.database import get_supabase
from app.models.network import NetworkStatsResponse
from app.services import network_stats_service

router = APIRouter(prefix="/v1/network", tags=["network"])


@router.get("/stats", response_model=NetworkStatsResponse)
async def network_stats(db: Client = Depends(get_supabase)):
    return NetworkStatsResponse(**network_stats_service.get_stats(db))
