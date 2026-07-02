"""End-to-end: storage over the cap returns 503 before consuming quota."""

from app.routes import images as images_route
from tests.integration.conftest import API_KEY


def test_storage_cap_blocks_with_503(image_env):
    client, db = image_env.client, image_env.db
    # Force the (cached) storage size over the cap.
    image_env.monkeypatch.setattr(
        images_route.storage_service, "current_size_mb", lambda: 999_999.0
    )

    resp = client.post(
        "/v1/images/generations",
        headers=API_KEY,
        json={"prompt": "x"},
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "storage_full"
    # Cap check runs before quota, so nothing was consumed.
    assert db._table("image_quota_usage").rows == []
