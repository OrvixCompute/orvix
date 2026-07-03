"""End-to-end: POST /v1/images/generations produces a file, row, and quota use."""

from datetime import datetime

from tests.integration.conftest import API_KEY


def test_full_image_flow(image_env):
    client, db, tmp_path = image_env.client, image_env.db, image_env.tmp_path
    wallet = db._table("users").rows[0]["wallet_address"]

    resp = client.post(
        "/v1/images/generations",
        headers=API_KEY,
        json={"model": "flux-schnell", "prompt": "a fox", "size": "1024x1024"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Response URL + quota headers.
    assert body["data"][0]["url"].endswith(".png")
    assert resp.headers["X-Orvix-Quota-Remaining"] == "4"  # 5/day holder, used 1
    assert "X-Orvix-Quota-Reset" in resp.headers

    # File written to disk.
    files = list(tmp_path.glob("*.png"))
    assert len(files) == 1
    assert files[0].read_bytes() == b"PNGDATA"

    # image_jobs row with a 24h expiry.
    rows = db._table("image_jobs").rows
    assert len(rows) == 1
    row = rows[0]
    created = datetime.fromisoformat(row["created_at"])
    expires = datetime.fromisoformat(row["expires_at"])
    assert abs((expires - created).total_seconds() - 24 * 3600) < 5

    # Quota consumed for the wallet.
    usage = db._table("image_quota_usage").rows
    assert len(usage) == 1
    assert usage[0]["wallet_address"] == wallet
    assert usage[0]["count"] == 1
