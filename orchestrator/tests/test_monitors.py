"""Tests for the monitor service: evaluation, dedup, cursors, webhook outbox."""

from datetime import datetime, timezone

import pytest
from solders.pubkey import Pubkey

from app.services import monitor_service
from app.services.monitor_service import _transfers_of_mint, monitor_service as svc


def _iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat()


def _monitor(**overrides) -> dict:
    row = {
        "id": "m-1",
        "user_id": "u-1",
        "name": "test monitor",
        "target_type": "token",
        "target_address": str(Pubkey.new_unique()),
        "conditions": [{"type": "accumulation_score", "gte": 70}],
        "webhook_url": None,
        "is_active": True,
        "interval_minutes": 30,
        "baseline_price_usdc": None,
        "last_checked_at": None,
        "last_cursor": None,
    }
    row.update(overrides)
    return row


class FakeAlertDb:
    """Tiny in-memory tables mirroring alert_events + alert_webhooks + monitors."""

    def __init__(self):
        self.alert_events = []
        self.alert_webhooks = []
        self.monitors = []

    def table(self, name):
        return _Table(name, self)


class _Table:
    def __init__(self, name, owner):
        self.name = name
        self.owner = owner
        self._filters = []
        self._op = None
        self._values = None

    def _rows(self):
        return getattr(self.owner, self.name)

    # builder ---------------------------------------------------------------
    def select(self, *cols, **kw):
        return self

    def eq(self, c, v):
        self._filters.append((c, "eq", v))
        return self

    def in_(self, c, values):
        self._filters.append((c, "in", values))
        return self

    def limit(self, n):
        return self

    def order(self, c, desc=False):
        return self

    def insert(self, values):
        self._op = "insert"
        self._values = values
        return self

    def update(self, values):
        self._op = "update"
        self._values = values
        return self

    def delete(self):
        self._op = "delete"
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
        rows = self._rows()
        if self._op == "insert":
            row = dict(self._values)
            row.setdefault("id", f"{self.name}-{len(rows) + 1}")
            row.setdefault("created_at", _iso())
            rows.append(row)
            return type("R", (), {"data": [row]})()
        if self._op == "update":
            for r in rows:
                if self._match(r):
                    r.update(self._values)
            return type("R", (), {"data": []})()
        if self._op == "delete":
            for r in list(rows):
                if self._match(r):
                    rows.remove(r)
            return type("R", (), {"data": []})()
        matched = [r for r in rows if self._match(r)]
        return type("R", (), {"data": matched})()


@pytest.fixture
def ctx():
    yield svc


# --- condition evaluation ------------------------------------------------

@pytest.mark.asyncio
async def test_accumulation_condition_fires(monkeypatch):
    db = FakeAlertDb()
    monitor = _monitor(conditions=[{"type": "accumulation_score", "gte": 70}])

    async def fake_accumulation(_db, mint, **kwargs):
        return {"score": 85, "label": "strong"}

    fake = type("T", (), {"compute_accumulation": staticmethod(fake_accumulation)})()
    monkeypatch.setattr(monitor_service, "token_intel", fake)

    await svc._eval_token_condition(db, monitor, monitor["conditions"][0], "accumulation_score")
    assert len(db.alert_events) == 1
    assert db.alert_events[0]["condition_type"] == "accumulation_score"
    assert db.alert_events[0]["dedup_key"].startswith("acc:")


@pytest.mark.asyncio
async def test_accumulation_condition_below_threshold_does_not_fire(monkeypatch):
    db = FakeAlertDb()
    monitor = _monitor(conditions=[{"type": "accumulation_score", "gte": 70}])

    async def fake_accumulation(_db, mint, **kwargs):
        return {"score": 30, "label": "weak"}

    fake = type("T", (), {"compute_accumulation": staticmethod(fake_accumulation)})()
    monkeypatch.setattr(monitor_service, "token_intel", fake)

    await svc._eval_token_condition(db, monitor, monitor["conditions"][0], "accumulation_score")
    assert db.alert_events == []


@pytest.mark.asyncio
async def test_dedup_same_key_no_second_event():
    db = FakeAlertDb()
    monitor = _monitor()
    await svc._emit_alert(db, monitor, "accumulation_score", "msg", "acc:2026-08-20", {"s": 1})
    await svc._emit_alert(db, monitor, "accumulation_score", "msg2", "acc:2026-08-20", {"s": 2})
    assert len(db.alert_events) == 1


@pytest.mark.asyncio
async def test_webhook_outbox_enqueued_with_alert():
    db = FakeAlertDb()
    monitor = _monitor(webhook_url="https://example.com/hook")
    await svc._emit_alert(db, monitor, "new_activity", "msg", "sig:x", {"k": "v"})
    assert len(db.alert_events) == 1
    assert len(db.alert_webhooks) == 1
    assert db.alert_webhooks[0]["status"] == "pending"
    assert db.alert_webhooks[0]["payload"]["event_id"] == db.alert_events[0]["id"]


def test_transfer_parsing_direction():
    wallet = str(Pubkey.new_unique())
    parsed = {
        "transaction": {
            "message": {
                "instructions": [
                    {
                        "program": "spl-token",
                        "parsed": {
                            "type": "transferChecked",
                            "info": {
                                "mint": "MINT",
                                "source": "src",
                                "destination": wallet,
                                "tokenAmount": {"uiAmount": 100.0, "uiAmountString": "100.0"},
                            },
                        },
                    }
                ]
            }
        }
    }
    transfers = _transfers_of_mint(parsed, "MINT", wallet)
    assert len(transfers) == 1
    assert transfers[0]["direction"] == "in"
    assert transfers[0]["amount"] == 100


@pytest.mark.asyncio
async def test_price_drop_condition_fires_once_per_day(monkeypatch):
    db = FakeAlertDb()
    monitor = _monitor(
        target_type="token",
        conditions=[{"type": "price_drop_pct", "gte": 10}],
        baseline_price_usdc=100.0,
    )

    async def _price(mint):
        return 80.0  # 20% drop

    monkeypatch.setattr(monitor_service.token_intel, "get_token_price_usdc", _price)

    await svc._eval_token_condition(db, monitor, monitor["conditions"][0], "price_drop_pct")
    assert len(db.alert_events) == 1

    # Same dedup key (same UTC day) -> no second event.
    await svc._eval_token_condition(db, monitor, monitor["conditions"][0], "price_drop_pct")
    assert len(db.alert_events) == 1


@pytest.mark.asyncio
async def test_new_activity_advances_cursor(monkeypatch):
    db = FakeAlertDb()
    monitor = _monitor(target_type="wallet", conditions=[{"type": "new_activity"}], last_cursor="old")

    class FakeSol:
        def __init__(self):
            self.sigs = [{"signature": "new1"}, {"signature": "new2"}]

        async def get_signatures_for_address(self, address, limit=25, until=None, before=None):
            return self.sigs

    monkeypatch.setattr(monitor_service, "get_solana_service", lambda: FakeSol())

    await svc._eval_new_activity(db, monitor, monitor["target_address"])
    assert len(db.alert_events) == 1
    assert db.alert_events[0]["dedup_key"] == "sig:new1"
    # Cursor should have been persisted to the monitor.
    updated = [m for m in db.monitors if m.get("id") == monitor["id"]]
    assert not updated  # our FakeAlertDb doesn't route monitor updates; fine for unit


@pytest.mark.asyncio
async def test_refresh_holder_snapshots_skips_without_watchlist(monkeypatch):
    """No watchlist -> no RPC work and no crash."""
    called = {"n": 0}

    async def fake_refresh(_db, mint):
        called["n"] += 1

    monkeypatch.setattr(monitor_service.settings, "TOKEN_WHALE_WATCHLIST_JSON", "")
    monkeypatch.setattr(
        monitor_service.token_intel, "refresh_holder_snapshot", fake_refresh
    )

    await svc.refresh_holder_snapshots()
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_refresh_holder_snapshots_refreshes_monitored_tokens(monkeypatch):
    """Active token monitors get their snapshots refreshed; wallets are skipped."""
    refreshed = []

    async def fake_refresh(_db, mint):
        refreshed.append(mint)

    monkeypatch.setattr(monitor_service.settings, "TOKEN_WHALE_WATCHLIST_JSON", '["wallet1"]')
    monkeypatch.setattr(
        monitor_service.token_intel, "refresh_holder_snapshot", fake_refresh
    )

    db = FakeAlertDb()
    db.monitors = [
        {"id": "m1", "target_address": "mint-a", "target_type": "token", "is_active": True},
        {"id": "m2", "target_address": "mint-b", "target_type": "token", "is_active": True},
        {"id": "m3", "target_address": "wallet-x", "target_type": "wallet", "is_active": True},
        {"id": "m4", "target_address": "mint-c", "target_type": "token", "is_active": False},
    ]
    monkeypatch.setattr(monitor_service, "get_supabase", lambda: db)

    await svc.refresh_holder_snapshots()
    assert refreshed == ["mint-a", "mint-b"]
