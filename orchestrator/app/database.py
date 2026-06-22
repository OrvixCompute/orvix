"""Singleton Supabase client and a FastAPI dependency to access it."""

from supabase import Client, create_client

from app.config import settings
from app.logger import logger

_client: Client | None = None


def get_supabase() -> Client:
    """Return the shared Supabase client, creating it on first use.

    Usable directly or as a FastAPI dependency: `db: Client = Depends(get_supabase)`.
    """
    global _client
    if _client is None:
        _client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
        logger.debug("Supabase client created for {}", settings.SUPABASE_URL)
    return _client


def test_connection() -> bool:
    """Probe the database with a trivial query. Returns True on success.

    Logs loudly on failure so startup problems are obvious.
    """
    try:
        client = get_supabase()
        # A lightweight existence check against the users table.
        client.table("users").select("id").limit(1).execute()
        logger.info("Supabase connection OK ({})", settings.SUPABASE_URL)
        return True
    except Exception as exc:  # noqa: BLE001 — we want to catch and report everything
        logger.error("Supabase connection FAILED: {}", exc)
        return False
