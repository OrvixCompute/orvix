"""API key management endpoints (JWT-authenticated)."""

from fastapi import APIRouter, Depends, status
from fastapi.responses import Response
from supabase import Client

from app.database import get_supabase
from app.dependencies import get_current_user
from app.models.api_key import (
    ApiKeyInfo,
    CreateApiKeyRequest,
    CreateApiKeyResponse,
)
from app.services.api_key_service import ApiKeyService

router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])


@router.post("", response_model=CreateApiKeyResponse, status_code=status.HTTP_201_CREATED)
async def create_key(
    body: CreateApiKeyRequest,
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    service = ApiKeyService(db)
    return service.create(current_user["id"], body.name)


@router.get("", response_model=list[ApiKeyInfo])
async def list_keys(
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    service = ApiKeyService(db)
    return [ApiKeyInfo.from_row(r) for r in service.list(current_user["id"])]


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_key(
    key_id: str,
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    service = ApiKeyService(db)
    service.revoke(current_user["id"], key_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{key_id}/rotate", response_model=CreateApiKeyResponse)
async def rotate_key(
    key_id: str,
    current_user: dict = Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    service = ApiKeyService(db)
    return service.rotate(current_user["id"], key_id)
