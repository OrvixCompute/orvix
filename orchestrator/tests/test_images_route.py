"""Endpoint tests for POST /v1/images/generations (node dispatch + fetch mocked)."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_supabase
from app.dependencies import get_user_from_api_key
from app.models.protocol import ImageJobCompleteMessage
from app.routes import images as images_route
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

    app = images_route.router  # noqa: F841 — ensure import side effects
    from app.main import app as fastapi_app

    fastapi_app.dependency_overrides[get_supabase] = lambda: db
    fastapi_app.dependency_overrides[get_user_from_api_key] = fake_user_dep

    # Treat the caller as a holder with the mint configured so the quota gate
    # allows up to IMAGE_DAILY_LIMIT_HOLDER/day (quota edges are in test_quota).
    monkeypatch.setattr(settings, "ORVX_MINT_ADDRESS", "MINT1111111111111111111111111111111111")

    async def fake_holder(db_, wallet):
        return True, 20000.0

    monkeypatch.setattr(holder_service, "get_holder_status", fake_holder)

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


def test_size_above_model_max_400(client_and_db):
    # orvix-image-1 tops out at 1024x1024; dispatching larger would only fail on
    # the node (OOM) after the request had already consumed a job slot.
    client, _ = client_and_db
    resp = client.post(
        "/v1/images/generations",
        headers={"Authorization": _KEY},
        json={"model": "orvix-image-1", "prompt": "x", "size": "1536x1536"},
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_size"
    assert "orvix-image-1" in error["message"]
    # The advertised choices are the ones this model can actually serve.
    assert "1536x1536" not in error["message"].split("Choose one of:")[1]


def test_size_allowed_when_model_declares_a_larger_max(client_and_db):
    client, _ = client_and_db
    resp = client.post(
        "/v1/images/generations",
        headers={"Authorization": _KEY},
        json={"model": "flux-schnell", "prompt": "x", "size": "1536x1536"},
    )
    assert resp.status_code == 200, resp.text


def test_size_no_model_supports_is_never_offered(client_and_db):
    # 1024x1792 is in the endpoint's vocabulary but exceeds every catalog model's
    # max_size — and the node protocol caps each dimension at 1536 regardless.
    client, _ = client_and_db
    resp = client.post(
        "/v1/images/generations",
        headers={"Authorization": _KEY},
        json={"model": "flux-schnell", "prompt": "x", "size": "1024x1792"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_size"


def test_size_is_case_and_whitespace_insensitive(client_and_db):
    client, _ = client_and_db
    resp = client.post(
        "/v1/images/generations",
        headers={"Authorization": _KEY},
        json={"prompt": "x", "size": " 512X512 "},
    )
    assert resp.status_code == 200, resp.text


# --- billing ---------------------------------------------------------------
#
# Images were free and providers were never paid for them. These pin the two
# halves that are easiest to get wrong: who gets charged, and what is given back
# when a request does not deliver everything it promised.


@pytest.fixture
def paid_client(client_and_db, monkeypatch):
    """A caller whose daily allowance is gone but who has USDC to spend."""
    client, db = client_and_db
    monkeypatch.setattr(settings, "IMAGE_DAILY_LIMIT_HOLDER", 0)
    monkeypatch.setattr(settings, "IMAGE_PRICE_USDC_PER_MEGAPIXEL", Decimal("0.01"))
    settled = []

    async def fake_settle(node, cost):
        settled.append(Decimal(str(cost)))
        return cost

    monkeypatch.setattr(node_manager, "settle_job", fake_settle)
    return client, db, settled


def test_free_image_is_not_charged(client_and_db, monkeypatch):
    client, db = client_and_db
    before = Decimal(str(db._table("users").rows[0]["balance_usdc"]))

    resp = client.post(
        "/v1/images/generations",
        headers={"Authorization": _KEY},
        json={"model": "orvix-image-1", "prompt": "a fox", "size": "1024x1024", "n": 1},
    )

    assert resp.status_code == 200, resp.text
    assert Decimal(str(db._table("users").rows[0]["balance_usdc"])) == before
    assert float(db._table("image_jobs").rows[0]["cost_usdc"]) == 0.0


def test_paid_image_deducts_and_pays_the_provider(paid_client):
    client, db, settled = paid_client
    before = Decimal(str(db._table("users").rows[0]["balance_usdc"]))

    resp = client.post(
        "/v1/images/generations",
        headers={"Authorization": _KEY},
        json={"model": "orvix-image-1", "prompt": "a fox", "size": "1024x1024", "n": 1},
    )

    assert resp.status_code == 200, resp.text
    after = Decimal(str(db._table("users").rows[0]["balance_usdc"]))
    charged = before - after
    assert charged > 0
    # gold tier carries a discount, so assert the recorded cost matches what was
    # actually taken rather than hard-coding a number the discount table owns.
    assert Decimal(str(db._table("image_jobs").rows[0]["cost_usdc"])) == charged
    # The provider is paid for images now, not only for chat.
    assert settled == [charged]


def test_price_scales_with_area(paid_client):
    """A 512x512 costs a quarter of a 1024x1024 — the GPU cost scales with area."""
    client, db, _ = paid_client

    def charge_for(size):
        before = Decimal(str(db._table("users").rows[0]["balance_usdc"]))
        r = client.post(
            "/v1/images/generations",
            headers={"Authorization": _KEY},
            json={"model": "orvix-image-1", "prompt": "x", "size": size, "n": 1},
        )
        assert r.status_code == 200, r.text
        return before - Decimal(str(db._table("users").rows[0]["balance_usdc"]))

    big = charge_for("1024x1024")
    small = charge_for("512x512")
    assert small * 4 == big


def test_paid_request_does_not_refund_quota_it_never_took(paid_client, monkeypatch):
    """The allowance was already exhausted, so there is nothing to give back.

    Refunding here would mint free images out of a failure.
    """
    client, db, _ = paid_client
    monkeypatch.setattr(node_manager, "select_image_node", lambda model: None)
    refunded = []
    monkeypatch.setattr(
        images_route.quota_service,
        "refund_image_quota",
        lambda db_, wallet, units: refunded.append(units),
    )

    resp = client.post(
        "/v1/images/generations",
        headers={"Authorization": _KEY},
        json={"model": "orvix-image-1", "prompt": "x", "size": "1024x1024", "n": 2},
    )

    assert resp.status_code == 503
    assert refunded == []


def test_partial_failure_charges_only_for_delivered_images(paid_client, monkeypatch):
    """Two requested, the second dies: exactly one image is paid for."""
    client, db, settled = paid_client
    before = Decimal(str(db._table("users").rows[0]["balance_usdc"]))
    calls = {"n": 0}

    async def flaky_dispatch(node, dispatch):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("node exploded")
        return ImageJobCompleteMessage(
            job_id=dispatch.job_id,
            image_id="img-1",
            binary_url="http://node/v1/binary/image/x",
            metadata={},
        )

    monkeypatch.setattr(node_manager, "dispatch_image_job", flaky_dispatch)

    resp = client.post(
        "/v1/images/generations",
        headers={"Authorization": _KEY},
        json={"model": "orvix-image-1", "prompt": "x", "size": "1024x1024", "n": 2},
    )

    assert resp.status_code == 502
    charged = before - Decimal(str(db._table("users").rows[0]["balance_usdc"]))
    assert len(settled) == 1, "only the delivered image should have been settled"
    assert charged == settled[0], "the caller paid for exactly what arrived"


def test_insufficient_balance_is_refused_before_any_work(paid_client, monkeypatch):
    client, db, settled = paid_client
    db._table("users").rows[0]["balance_usdc"] = 0.000001
    dispatched = []
    monkeypatch.setattr(
        node_manager, "dispatch_image_job", lambda *a, **k: dispatched.append(1)
    )

    resp = client.post(
        "/v1/images/generations",
        headers={"Authorization": _KEY},
        json={"model": "orvix-image-1", "prompt": "x", "size": "1024x1024", "n": 1},
    )

    assert resp.status_code == 402
    assert dispatched == [], "no GPU work for a request that cannot pay"
    assert settled == []
