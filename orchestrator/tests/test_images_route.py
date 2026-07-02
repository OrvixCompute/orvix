"""Endpoint tests for POST /v1/images/generations (node dispatch + fetch mocked)."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_supabase
from app.dependencies import get_user_from_api_key
from app.models.protocol import ImageJobCompleteMessage
from app.routes import images as images_route
from app.services.node_manager import node_manager
from tests.fakes import FakeSupabase

_KEY = "Bearer orvx_sk_testkey0testkey0testkey0testkey0"


@pytest.fixture
def client_and_db(tmp_path, monkeypatch):
    db = FakeSupabase()
    db.add_user(tier="gold", balance_usdc=100.0)

    def fake_user_dep():
        return {
            "user": db._table("users").rows[0],
            "api_key": {"id": "key-0", "user_id": db._table("users").rows[0]["id"]},
        }

    app = images_route.router  # noqa: F841 — ensure import side effects
    from app.main import app as fastapi_app

    fastapi_app.dependency_overrides[get_supabase] = lambda: db
    fastapi_app.dependency_overrides[get_user_from_api_key] = fake_user_dep

    # Save images into a temp dir and stub the binary fetch (no real node).
    monkeypatch.setattr(settings, "IMAGE_STORAGE_DIR", str(tmp_path))

    async def fake_fetch(url, token):
        return b"PNGDATA"

    monkeypatch.setattr(images_route, "_fetch_image_bytes", fake_fetch)

    fake_node = SimpleNamespace(provider_id="prov-1", node_id="node-1")
    monkeypatch.setattr(node_manager, "select_image_node", lambda model: fake_node)

    async def fake_dispatch(node, dispatch):
        return ImageJobCompleteMessage(
            job_id=dispatch.job_id,
            image_id=f"img-{dispatch.job_id}",
            binary_url="http://node/v1/binary/image/x",
            metadata={},
        )

    monkeypatch.setattr(node_manager, "dispatch_image_job", fake_dispatch)

    client = TestClient(fastapi_app)
    yield client, db
    fastapi_app.dependency_overrides.clear()


def test_generates_url(client_and_db, tmp_path):
    client, db = client_and_db
    resp = client.post(
        "/v1/images/generations",
        headers={"Authorization": _KEY},
        json={"model": "flux-schnell", "prompt": "a cat", "size": "512x512"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["url"].startswith(settings.PUBLIC_IMAGE_URL_BASE)
    # A file was written and a job row recorded.
    assert len(list(tmp_path.glob("*.png"))) == 1
    rows = db._table("image_jobs").rows
    assert len(rows) == 1
    assert rows[0]["width"] == 512 and rows[0]["cost_usdc"] == 0


def test_b64_response_format(client_and_db):
    client, _ = client_and_db
    resp = client.post(
        "/v1/images/generations",
        headers={"Authorization": _KEY},
        json={"prompt": "x", "response_format": "b64_json"},
    )
    assert resp.status_code == 200
    import base64

    assert base64.b64decode(resp.json()["data"][0]["b64_json"]) == b"PNGDATA"


def test_n_multiple_images(client_and_db):
    client, db = client_and_db
    resp = client.post(
        "/v1/images/generations",
        headers={"Authorization": _KEY},
        json={"prompt": "x", "n": 3},
    )
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 3
    assert len(db._table("image_jobs").rows) == 3


def test_invalid_model_400(client_and_db):
    client, _ = client_and_db
    resp = client.post(
        "/v1/images/generations",
        headers={"Authorization": _KEY},
        json={"model": "qwen-2.5-7b", "prompt": "x"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "model_not_found"


def test_no_provider_503(client_and_db, monkeypatch):
    client, _ = client_and_db
    monkeypatch.setattr(node_manager, "select_image_node", lambda model: None)
    resp = client.post(
        "/v1/images/generations",
        headers={"Authorization": _KEY},
        json={"prompt": "x"},
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "no_image_provider"


def test_invalid_size_400(client_and_db):
    client, _ = client_and_db
    resp = client.post(
        "/v1/images/generations",
        headers={"Authorization": _KEY},
        json={"prompt": "x", "size": "999x999"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_size"
