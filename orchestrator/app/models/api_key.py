"""Pydantic models for API key management."""

from datetime import datetime

from pydantic import BaseModel, Field


class CreateApiKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Human-friendly key label")


class CreateApiKeyResponse(BaseModel):
    """Returned ONCE on creation/rotation — includes the plaintext key."""

    id: str
    key: str = Field(..., description="Full plaintext key — shown only once")
    prefix: str
    name: str
    created_at: datetime


class ApiKeyInfo(BaseModel):
    """Listing representation — never includes the hash or full key."""

    id: str
    prefix: str
    name: str
    is_active: bool
    last_used_at: datetime | None = None
    created_at: datetime

    @classmethod
    def from_row(cls, row: dict) -> "ApiKeyInfo":
        return cls(
            id=str(row["id"]),
            prefix=row["key_prefix"],
            name=row["name"],
            is_active=row["is_active"],
            last_used_at=row.get("last_used_at"),
            created_at=row["created_at"],
        )
