"""End-to-end: a node failure refunds the consumed image quota."""

from app.services.node_manager import node_manager
from tests.integration.conftest import API_KEY


def test_quota_refunded_on_node_failure(image_env):
    client, db = image_env.client, image_env.db

    async def failing_dispatch(node, dispatch):
        raise RuntimeError("engine boom")

    image_env.monkeypatch.setattr(node_manager, "dispatch_image_job", failing_dispatch)

    resp = client.post(
        "/v1/images/generations",
        headers=API_KEY,
        json={"prompt": "x", "n": 2},
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "node_error"

    # All 2 consumed units were refunded (no image produced).
    usage = db._table("image_quota_usage").rows
    assert len(usage) == 1
    assert usage[0]["count"] == 0


def test_quota_refunded_when_no_provider(image_env):
    client, db = image_env.client, image_env.db
    image_env.monkeypatch.setattr(node_manager, "select_image_node", lambda m: None)

    resp = client.post(
        "/v1/images/generations",
        headers=API_KEY,
        json={"prompt": "x", "n": 3},
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "no_image_provider"
    usage = db._table("image_quota_usage").rows
    assert len(usage) == 1
    assert usage[0]["count"] == 0
