"""Tests for webhook outbox delivery (retry/backoff in monitor_service)."""

import pytest

from app.services import monitor_service
from app.services.monitor_service import monitor_service as svc


def _webhook_row(**overrides) -> dict:
    row = {
        "id": "w-1",
        "alert_event_id": "e-1",
        "monitor_id": "m-1",
        "webhook_url": "https://example.com/hook",
        "payload": {"event_id": "e-1", "message": "hello"},
        "status": "pending",
        "attempts": 0,
        "next_retry_at": None,
        "last_error": None,
    }
    row.update(overrides)
    return row


class FakeWebhookDb:
    """In-memory alert_webhooks table supporting read + write chains."""

    def __init__(self):
        self.rows = []

    def table(self, name):
        assert name == "alert_webhooks"
        return _Table(self)

    def _apply(self, predicate, values):
        for row in self.rows:
            if predicate(row):
                row.update(values)


class _Table:
    def __init__(self, db):
        self.db = db
        self._filters = []
        self._op = None
        self._values = None

    # builder ---------------------------------------------------------------
    def select(self, *a, **k):
        return self

    def in_(self, c, values):
        self._filters.append((c, "in", values))
        return self

    def eq(self, c, v):
        self._filters.append((c, "eq", v))
        return self

    def update(self, values):
        self._op = "update"
        self._values = values
        return self

    def _match(self, row):
        for c, op, v in self._filters:
            if op == "eq" and row.get(c) != v:
                return False
            if op == "in" and row.get(c) not in v:
                return False
        return True

    # execution -------------------------------------------------------------
    def execute(self):
        if self._op == "update":
            for row in self.db.rows:
                if self._match(row):
                    row.update(self._values)
            return type("R", (), {"data": []})()
        rows = [r for r in self.db.rows if self._match(r)]
        return type("R", (), {"data": rows})()


@pytest.mark.asyncio
async def test_success_marks_delivered(monkeypatch):
    db = FakeWebhookDb()
    db.rows.append(_webhook_row())

    class FakeResp:
        status_code = 200

    async def fake_post(url, json, timeout, headers=None):
        return FakeResp()

    monkeypatch.setattr(monitor_service.httpx, "AsyncClient", _client(fake_post))

    await svc._deliver_webhook(db, db.rows[0])
    assert db.rows[0]["status"] == "delivered"
    assert db.rows[0]["attempts"] == 1


@pytest.mark.asyncio
async def test_failure_backs_off(monkeypatch):
    db = FakeWebhookDb()
    db.rows.append(_webhook_row())

    class FakeResp:
        status_code = 500

    async def fake_post(url, json, timeout, headers=None):
        return FakeResp()

    monkeypatch.setattr(monitor_service.httpx, "AsyncClient", _client(fake_post))

    await svc._deliver_webhook(db, db.rows[0])
    row = db.rows[0]
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["next_retry_at"] is not None  # backoff scheduled


@pytest.mark.asyncio
async def test_failure_caps_at_max_attempts(monkeypatch):
    db = FakeWebhookDb()
    db.rows.append(_webhook_row(attempts=4))  # 5th attempt is the cap

    class FakeResp:
        status_code = 503

    async def fake_post(url, json, timeout, headers=None):
        return FakeResp()

    monkeypatch.setattr(monitor_service.httpx, "AsyncClient", _client(fake_post))

    await svc._deliver_webhook(db, db.rows[0])
    row = db.rows[0]
    assert row["status"] == "failed"
    assert row["attempts"] == 5


@pytest.mark.asyncio
async def test_network_error_is_retryable(monkeypatch):
    db = FakeWebhookDb()
    db.rows.append(_webhook_row())

    async def fake_post(url, json, timeout, headers=None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(monitor_service.httpx, "AsyncClient", _client(fake_post))

    await svc._deliver_webhook(db, db.rows[0])
    row = db.rows[0]
    assert row["status"] == "pending"
    assert row["last_error"] == "connection refused"


def test_sign_payload_is_deterministic_and_keyed():
    payload = {"a": 1, "b": {"c": "x"}}
    sig1 = monitor_service._sign_payload(payload, "secret1")
    sig2 = monitor_service._sign_payload(payload, "secret1")
    sig3 = monitor_service._sign_payload(payload, "secret2")
    assert sig1 == sig2
    assert sig1 != sig3
    assert len(sig1) == 64  # hex sha256


def test_sign_payload_matches_manual_hmac():
    import hashlib
    import hmac
    import json

    payload = {"event_id": "e-1", "message": "hi"}
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    expected = hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    assert monitor_service._sign_payload(payload, "topsecret") == expected


@pytest.mark.asyncio
async def test_signing_headers_sent_when_secret_configured(monkeypatch):
    """The delivery includes X-Orvix-Signature when WEBHOOK_SIGNING_SECRET is set."""
    db = FakeWebhookDb()
    db.rows.append(_webhook_row())
    captured = {}

    class FakeResp:
        status_code = 200

    async def fake_post(url, json, timeout, headers=None):
        captured["headers"] = headers or {}
        return FakeResp()

    monkeypatch.setattr(monitor_service.httpx, "AsyncClient", _client(fake_post))
    monkeypatch.setattr(monitor_service.settings, "WEBHOOK_SIGNING_SECRET", "sekret")

    await svc._deliver_webhook(db, db.rows[0])
    sig = captured["headers"].get("X-Orvix-Signature")
    assert sig is not None
    assert len(sig) == 64


@pytest.mark.asyncio
async def test_no_signing_headers_without_secret(monkeypatch):
    db = FakeWebhookDb()
    db.rows.append(_webhook_row())
    captured = {}

    class FakeResp:
        status_code = 200

    async def fake_post(url, json, timeout, headers=None):
        captured["headers"] = headers or {}
        return FakeResp()

    monkeypatch.setattr(monitor_service.httpx, "AsyncClient", _client(fake_post))
    monkeypatch.setattr(monitor_service.settings, "WEBHOOK_SIGNING_SECRET", "")

    await svc._deliver_webhook(db, db.rows[0])
    assert "X-Orvix-Signature" not in captured["headers"]


def _client(post_fn):
    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, timeout, headers=None):
            return await post_fn(url, json, timeout, headers=headers)

    return FakeClient
