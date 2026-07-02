"""Node binary endpoint: auth, streaming, and delete-after-transfer."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orvix_node import binary
from orvix_node.health import create_health_app


@pytest.fixture(autouse=True)
def clear_registry():
    binary._registry.clear()
    yield
    binary._registry.clear()


def _client():
    return TestClient(create_health_app())


def test_fetch_streams_and_deletes(tmp_path):
    path = tmp_path / "img.png"
    path.write_bytes(b"PNGDATA")
    binary.register_image("img1", "tok123", str(path))

    r = _client().get("/v1/binary/image/img1", headers={"X-Node-Secret": "tok123"})
    assert r.status_code == 200
    assert r.content == b"PNGDATA"
    # Served exactly once: file + registry entry removed by the background task.
    assert not path.exists()
    assert "img1" not in binary._registry


def test_wrong_token_401(tmp_path):
    path = tmp_path / "img.png"
    path.write_bytes(b"X")
    binary.register_image("img2", "right", str(path))

    r = _client().get("/v1/binary/image/img2", headers={"X-Node-Secret": "wrong"})
    assert r.status_code == 401
    assert path.exists()  # not deleted on failed auth


def test_missing_token_401(tmp_path):
    path = tmp_path / "img.png"
    path.write_bytes(b"X")
    binary.register_image("img3", "right", str(path))
    r = _client().get("/v1/binary/image/img3")
    assert r.status_code == 401


def test_unknown_image_404():
    r = _client().get("/v1/binary/image/nope", headers={"X-Node-Secret": "x"})
    assert r.status_code == 404
