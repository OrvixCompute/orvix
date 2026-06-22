"""Business logic for creating, listing, revoking, and rotating API keys."""

import hashlib
import secrets

from supabase import Client

from app.exceptions import NotFoundError, ValidationError
from app.logger import logger

KEY_PREFIX = "orvx_sk_"
MAX_ACTIVE_KEYS = 10
DISPLAY_PREFIX_LEN = 12  # e.g. "orvx_sk_abcd"


def generate_key() -> str:
    """Generate a new plaintext API key: orvx_sk_<32-char urlsafe>."""
    return f"{KEY_PREFIX}{secrets.token_urlsafe(32)[:32]}"


def hash_key(key: str) -> str:
    """Return the sha256 hex digest used for storage and lookup."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class ApiKeyService:
    def __init__(self, db: Client) -> None:
        self.db = db

    def _active_count(self, user_id: str) -> int:
        res = (
            self.db.table("api_keys")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("is_active", True)
            .execute()
        )
        return res.count or 0

    def create(self, user_id: str, name: str) -> dict:
        """Create a key, enforcing the per-user active limit. Returns plaintext once."""
        if self._active_count(user_id) >= MAX_ACTIVE_KEYS:
            raise ValidationError(
                f"Maximum of {MAX_ACTIVE_KEYS} active API keys reached. "
                "Delete one before creating another."
            )

        plaintext = generate_key()
        row = {
            "user_id": user_id,
            "key_hash": hash_key(plaintext),
            "key_prefix": plaintext[:DISPLAY_PREFIX_LEN],
            "name": name,
            "is_active": True,
        }
        res = self.db.table("api_keys").insert(row).execute()
        created = res.data[0]
        logger.info("Created API key {} for user {}", created["id"], user_id)
        return {
            "id": str(created["id"]),
            "key": plaintext,
            "prefix": created["key_prefix"],
            "name": created["name"],
            "created_at": created["created_at"],
        }

    def list(self, user_id: str) -> list[dict]:
        """Return all of a user's keys, newest first. Never exposes the hash."""
        res = (
            self.db.table("api_keys")
            .select("id, key_prefix, name, is_active, last_used_at, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return res.data or []

    def _get_owned(self, user_id: str, key_id: str) -> dict:
        res = (
            self.db.table("api_keys")
            .select("*")
            .eq("id", key_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if not res.data:
            raise NotFoundError("API key not found")
        return res.data[0]

    def revoke(self, user_id: str, key_id: str) -> None:
        """Soft-delete a key the user owns (is_active = false)."""
        self._get_owned(user_id, key_id)  # ownership check (raises if not found)
        self.db.table("api_keys").update({"is_active": False}).eq("id", key_id).execute()
        logger.info("Revoked API key {} for user {}", key_id, user_id)

    def rotate(self, user_id: str, key_id: str) -> dict:
        """Deactivate an existing key and issue a fresh one with the same name."""
        old = self._get_owned(user_id, key_id)
        self.db.table("api_keys").update({"is_active": False}).eq("id", key_id).execute()
        logger.info("Rotating API key {} for user {}", key_id, user_id)
        return self.create(user_id, old["name"])
