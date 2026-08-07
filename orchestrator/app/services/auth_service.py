"""Wallet authentication: challenge nonces, ed25519 verification, and JWTs."""

import secrets
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from solders.pubkey import Pubkey
from solders.signature import Signature
from supabase import Client

from app.config import settings
from app.exceptions import UnauthorizedError, ValidationError
from app.logger import logger

# Required prefix — signing arbitrary data is rejected.
MESSAGE_PREFIX = "Sign this message to authenticate with Orvix"
CHALLENGE_TTL_MINUTES = 5
MAX_MESSAGE_AGE_MINUTES = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value) -> datetime:
    """Parse a timestamptz coming back from PostgREST into an aware datetime."""
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class AuthService:
    """Stateless — challenges live in the `auth_challenges` table.

    They used to sit in an in-memory dict, which lost every pending challenge on
    restart and, being keyed by wallet, let a second /challenge call silently
    invalidate the first. Both surfaced to users as the same opaque 401. See
    migration 017.
    """

    # --- Challenge ---------------------------------------------------------
    def create_challenge(self, db: Client, wallet: str) -> dict:
        """Generate a nonce + message for the wallet to sign."""
        self._validate_wallet(wallet)

        nonce = secrets.token_hex(16)  # 32 hex chars
        issued = _now()
        expires_at = issued + timedelta(minutes=CHALLENGE_TTL_MINUTES)
        message = (
            f"{MESSAGE_PREFIX}.\n"
            f"Nonce: {nonce}\n"
            f"Timestamp: {issued.isoformat()}"
        )

        # Opportunistic sweep: expired rows are dead weight and nothing else
        # deletes them. Bounded by the index on expires_at and best-effort — a
        # failed cleanup must never stop a user from logging in.
        try:
            db.table("auth_challenges").delete().lt("expires_at", _now().isoformat()).execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Expired-challenge sweep failed: {}", exc)

        db.table("auth_challenges").insert(
            {
                "nonce": nonce,
                "wallet": wallet,
                "expires_at": expires_at.isoformat(),
            }
        ).execute()
        logger.debug("Issued challenge for {} (nonce={})", wallet, nonce)
        return {"message": message, "nonce": nonce, "expires_at": expires_at}

    # --- Verification ------------------------------------------------------
    def verify_signature(self, db: Client, wallet: str, message: str, signature: str) -> None:
        """Validate message format, nonce, freshness, and the ed25519 signature.

        Raises UnauthorizedError / ValidationError on any failure. On success the
        nonce is consumed (single-use).
        """
        self._validate_wallet(wallet)

        if not message.startswith(MESSAGE_PREFIX):
            raise ValidationError("Message does not have the required Orvix prefix")

        nonce = self._extract_field(message, "Nonce")
        timestamp_str = self._extract_field(message, "Timestamp")

        # Look the challenge up by its nonce, then confirm it was issued to this
        # wallet. Looking up by wallet instead would mean only the newest
        # challenge is ever valid, which is exactly the bug migration 017 fixes.
        found = (
            db.table("auth_challenges")
            .select("*")
            .eq("nonce", nonce)
            .limit(1)
            .execute()
            .data
        )
        stored = found[0] if found else None
        if not stored or stored["wallet"] != wallet:
            raise UnauthorizedError("Unknown or already-used challenge nonce")
        if _now() > _parse_ts(stored["expires_at"]):
            db.table("auth_challenges").delete().eq("nonce", nonce).execute()
            raise UnauthorizedError("Challenge has expired")

        # Reject stale messages even if the nonce somehow lingers.
        try:
            issued = datetime.fromisoformat(timestamp_str)
        except ValueError as exc:
            raise ValidationError("Malformed timestamp in message") from exc
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=timezone.utc)
        if _now() - issued > timedelta(minutes=MAX_MESSAGE_AGE_MINUTES):
            raise UnauthorizedError("Message timestamp is too old")

        # Cryptographic verification.
        if not self._verify_ed25519(wallet, message, signature):
            raise UnauthorizedError("Signature verification failed")

        # One-time use — consume the nonce.
        db.table("auth_challenges").delete().eq("nonce", nonce).execute()
        logger.info("Signature verified for wallet {}", wallet)

    # --- JWT ---------------------------------------------------------------
    def create_jwt(self, user: dict) -> str:
        """Issue an HS256 JWT for the given user row."""
        now = _now()
        payload = {
            "sub": str(user["id"]),
            "wallet": user["wallet_address"],
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=settings.JWT_EXPIRY_HOURS)).timestamp()),
        }
        return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)

    def verify_jwt(self, token: str) -> dict:
        """Decode and validate a JWT, returning its claims. Raises on failure."""
        try:
            return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        except JWTError as exc:
            raise UnauthorizedError(f"Invalid or expired token: {exc}") from exc

    # --- Internals ---------------------------------------------------------
    @staticmethod
    def _validate_wallet(wallet: str) -> None:
        try:
            Pubkey.from_string(wallet)
        except Exception as exc:  # noqa: BLE001
            raise ValidationError("Invalid Solana wallet address") from exc

    @staticmethod
    def _extract_field(message: str, label: str) -> str:
        """Pull a `Label: value` line out of the challenge message."""
        for line in message.splitlines():
            if line.startswith(f"{label}:"):
                return line.split(":", 1)[1].strip()
        raise ValidationError(f"Message missing required field: {label}")

    @staticmethod
    def _verify_ed25519(wallet: str, message: str, signature: str) -> bool:
        """Verify a base58 ed25519 signature against the wallet's public key."""
        try:
            pubkey = Pubkey.from_string(wallet)
            sig = Signature.from_string(signature)
            return sig.verify(pubkey, message.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("ed25519 verification error: {}", exc)
            return False


# Module-level singleton. Holds no state now that challenges are in the database;
# kept as a singleton so callers keep the same import path.
auth_service = AuthService()
