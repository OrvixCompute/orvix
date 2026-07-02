"""Shared fixture for image-generation end-to-end tests.

Wires the app with an in-memory DB and mocks the node dispatch + binary fetch, so
the full POST /v1/images/generations path runs without a real node or GPU.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_supabase
from app.dependencies import get_user_from_api_key
from app.main import app
from app.models.protocol import ImageJobCompleteMessage
from app.routes import images as images_route
from app.services.holder import holder_service
from app.services.node_manager import node_manager
from tests.fakes import FakeSupabase

API_KEY = {"Authorization": "Bearer orvx_sk_testkey0testkey0testkey0testkey0"}


@pytest.fixture
def image_env(tmp_path, monkeypatch):
    db = FakeSupabase()
    db.add_user()

    def dep():
        return {
            "user": db._table("users").rows[0],
            "api_key": {"id": "k-e2e", "user_id": db._table("users").rows[0]["id"]},
        }

    async def fake_holder(d, w):
        return True, 20000.0

    monkeypatch.setattr(holder_service, "get_holder_status", fake_holder)
    monkeypatch.setattr(settings, "ORVX_MINT_ADDRESS", "MINT")
    monkeypatch.setattr(settings, "IMAGE_STORAGE_DIR", str(tmp_path))

    async def fake_fetch(url, token):
        return b"PNGDATA"

    monkeypatch.setattr(images_route, "_fetch_image_bytes", fake_fetch)

    fake_node = SimpleNamespace(provider_id="prov-1", node_id="node-1")
    monkeypatch.setattr(node_manager, "select_image_node", lambda m: fake_node)

    async def fake_dispatch(node, dispatch):
        return ImageJobCompleteMessage(
            job_id=dispatch.job_id,
            image_id=f"img-{dispatch.job_id}",
            binary_url="http://node/v1/binary/image/x",
            metadata={},
        )

    monkeypatch.setattr(node_manager, "dispatch_image_job", fake_dispatch)

    app.dependency_overrides[get_supabase] = lambda: db
    app.dependency_overrides[get_user_from_api_key] = dep
    client = TestClient(app)
    yield SimpleNamespace(client=client, db=db, tmp_path=tmp_path, monkeypatch=monkeypatch)
    app.dependency_overrides.clear()
