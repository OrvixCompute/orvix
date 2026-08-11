"""Endpoint tests for POST /v1/videos/generations (node dispatch + fetch mocked)."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_supabase
from app.dependencies import get_user_from_api_key
from app.models.protocol import VideoJobCompleteMessage
from app.routes import videos as videos_route
from app.services.holder import holder_service
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

    from app.main import app as fastapi_app

    fastapi_app.dependency_overrides[get_supabase] = lambda: db
    fastapi_app.dependency_overrides[get_user_from_api_key] = fake_user_dep

    # Treat the caller as a holder with the mint configured so the quota gate
    # allows up to VIDEO_DAILY_LIMIT_HOLDER/day (quota edges are in test_quota).
    monkeypatch.setattr(settings, "ORVX_MINT_ADDRESS", "MINT1111111111111111111111111111111111")

    async def fake_holder(db_, wallet):
        return True, 20000.0

    monkeypatch.setattr(holder_service, "get_holder_status", fake_holder)

    # Save videos into a temp dir and stub the binary fetch (no real node).
    monkeypatch.setattr(settings, "VIDEO_STORAGE_DIR", str(tmp_path))

    async def fake_fetch(url, token):
        return b"MP4DATA"

    monkeypatch.setattr(videos_route, "_fetch_video_bytes", fake_fetch)

    fake_node = SimpleNamespace(provider_id="prov-1", node_id="node-1")
    monkeypatch.setattr(node_manager, "select_video_node", lambda model: fake_node)

    async def fake_dispatch(node, dispatch):
        return VideoJobCompleteMessage(
            job_id=dispatch.job_id,
            video_id=f"vid-{dispatch.job_id}",
            binary_url="http://node/v1/binary/video/x",
            metadata={},
        )

    monkeypatch.setattr(node_manager, "dispatch_video_job", fake_dispatch)

    client = TestClient(fastapi_app)
    yield client, db
    fastapi_app.dependency_overrides.clear()


def _post(client, **overrides):
    body = {"model": "orvix-video-1", "prompt": "a cat walking", **overrides}
    return client.post(
        "/v1/videos/generations", headers={"Authorization": _KEY}, json=body
    )


def test_generates_url(client_and_db, tmp_path):
    client, db = client_and_db
    resp = _post(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["url"].startswith(settings.PUBLIC_VIDEO_URL_BASE)
    # A file was written and a job row recorded.
    assert len(list(tmp_path.glob("*.mp4"))) == 1
    rows = db._table("video_jobs").rows
    assert len(rows) == 1
    assert rows[0]["width"] == 704 and rows[0]["cost_usdc"] == 0


def test_quota_headers_present(client_and_db):
    client, _ = client_and_db
    resp = _post(client)
    assert resp.status_code == 200, resp.text
    assert "X-Orvix-Quota-Remaining" in resp.headers
    assert "X-Orvix-Quota-Reset" in resp.headers


def test_invalid_model_400(client_and_db):
    client, _ = client_and_db
    resp = _post(client, model="qwen-2.5-7b")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "model_not_found"


def test_resolution_above_model_max_400(client_and_db):
    # orvix-video-1 tops out at 1280x720 in the catalog; the request model
    # enforces the same bound, so an over-limit request is refused (422) before
    # any quota is consumed.
    client, _ = client_and_db
    resp = _post(client, width=1280, height=1280)
    assert resp.status_code == 422


def test_resolution_at_model_max_ok(client_and_db):
    client, _ = client_and_db
    resp = _post(client, width=1280, height=720)
    assert resp.status_code == 200, resp.text


def test_request_bounds_enforced_by_model(client_and_db):
    # The request model itself bounds width/height/frames etc.
    client, _ = client_and_db
    resp = _post(client, width=9999)
    assert resp.status_code == 422


def test_no_provider_503(client_and_db, monkeypatch):
    client, _ = client_and_db
    monkeypatch.setattr(node_manager, "select_video_node", lambda model: None)
    monkeypatch.setattr(node_manager, "unavailable_reason", lambda model, engine=None: "no_node")
    resp = _post(client)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "no_video_provider"


def test_capacity_exhausted_503(client_and_db, monkeypatch):
    client, _ = client_and_db
    monkeypatch.setattr(node_manager, "select_video_node", lambda model: None)
    monkeypatch.setattr(node_manager, "unavailable_reason", lambda model, engine=None: "at_capacity")
    resp = _post(client)
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "capacity_exhausted"
    assert "retry_after_seconds" in body["error"]


def test_no_provider_refunds_quota(client_and_db, monkeypatch):
    client, db = client_and_db
    monkeypatch.setattr(node_manager, "select_video_node", lambda model: None)
    monkeypatch.setattr(node_manager, "unavailable_reason", lambda model, engine=None: "no_node")
    refunded = []
    monkeypatch.setattr(
        videos_route.quota_service,
        "refund_video_quota",
        lambda db_, wallet, units: refunded.append(units),
    )

    resp = _post(client)
    assert resp.status_code == 503
    assert refunded == [1]


def test_dispatch_timeout_504_refunds_quota(client_and_db, monkeypatch):
    client, _ = client_and_db
    from app.services.node_manager import NodeTimeoutError

    async def slow_dispatch(node, dispatch):
        raise NodeTimeoutError("timed out")

    monkeypatch.setattr(node_manager, "dispatch_video_job", slow_dispatch)
    refunded = []
    monkeypatch.setattr(
        videos_route.quota_service,
        "refund_video_quota",
        lambda db_, wallet, units: refunded.append(units),
    )

    resp = _post(client)
    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "node_timeout"
    assert refunded == [1]


def test_dispatch_node_error_502_refunds_quota(client_and_db, monkeypatch):
    client, _ = client_and_db

    async def failing_dispatch(node, dispatch):
        raise RuntimeError("node exploded")

    monkeypatch.setattr(node_manager, "dispatch_video_job", failing_dispatch)
    refunded = []
    monkeypatch.setattr(
        videos_route.quota_service,
        "refund_video_quota",
        lambda db_, wallet, units: refunded.append(units),
    )

    resp = _post(client)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "node_error"
    assert refunded == [1]


def test_fetch_failure_502_refunds_quota(client_and_db, monkeypatch):
    client, _ = client_and_db
    import httpx

    async def broken_fetch(url, token):
        raise httpx.HTTPError("no route to node")

    monkeypatch.setattr(videos_route, "_fetch_video_bytes", broken_fetch)
    refunded = []
    monkeypatch.setattr(
        videos_route.quota_service,
        "refund_video_quota",
        lambda db_, wallet, units: refunded.append(units),
    )

    resp = _post(client)
    assert resp.status_code == 502
    assert refunded == [1]
