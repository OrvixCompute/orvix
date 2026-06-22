"""A minimal in-memory stand-in for the Supabase client.

Supports just enough of the postgrest query builder + rpc() for the services and
routes under test — no network, no real database required.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal


class _Result:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class FakeTable:
    def __init__(self, name: str):
        self.name = name
        self.rows: list[dict] = []

    def insert_row(self, row: dict) -> dict:
        r = dict(row)
        r.setdefault("id", str(uuid.uuid4()))
        r.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        self.rows.append(r)
        return r


class _Query:
    def __init__(self, table: FakeTable):
        self.t = table
        self._op = "select"
        self._cols = None  # None == all columns
        self._count = None
        self._filters: list[tuple] = []
        self._values = None
        self._order = None
        self._desc = False
        self._limit = None
        self._range = None

    # builder ---------------------------------------------------------------
    def select(self, *cols, count=None):
        self._op, self._count = "select", count
        # Mirror postgrest column projection: select("a, b, c") or select("*").
        if cols and cols[0] not in ("*", ""):
            self._cols = [c.strip() for c in cols[0].split(",") if c.strip()]
        else:
            self._cols = None
        return self

    def _project(self, row: dict) -> dict:
        if self._cols is None:
            return dict(row)
        return {c: row.get(c) for c in self._cols}

    def insert(self, values):
        self._op, self._values = "insert", values
        return self

    def upsert(self, values):
        self._op, self._values = "upsert", values
        return self

    def update(self, values):
        self._op, self._values = "update", values
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, c, v):
        self._filters.append((c, "eq", v))
        return self

    def neq(self, c, v):
        self._filters.append((c, "neq", v))
        return self

    def gt(self, c, v):
        self._filters.append((c, "gt", v))
        return self

    def gte(self, c, v):
        self._filters.append((c, "gte", v))
        return self

    def in_(self, c, values):
        self._filters.append((c, "in", list(values)))
        return self

    def contains(self, c, value):
        self._filters.append((c, "contains", value))
        return self

    def order(self, c, desc=False):
        self._order, self._desc = c, desc
        return self

    def limit(self, n):
        self._limit = n
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    # execution -------------------------------------------------------------
    def _match(self, row) -> bool:
        for c, op, v in self._filters:
            cell = row.get(c)
            if op == "eq" and cell != v:
                return False
            if op == "neq" and cell == v:
                return False
            if op == "gt" and not (cell is not None and cell > v):
                return False
            if op == "gte" and not (cell is not None and cell >= v):
                return False
            if op == "in" and cell not in v:
                return False
            if op == "contains":
                # jsonb @> : every key/value in v must be present in cell.
                if not isinstance(cell, dict):
                    return False
                if not all(cell.get(k) == val for k, val in v.items()):
                    return False
        return True

    def execute(self):
        if self._op == "insert":
            rows = self._values if isinstance(self._values, list) else [self._values]
            return _Result([self.t.insert_row(r) for r in rows])

        if self._op == "upsert":
            rows = self._values if isinstance(self._values, list) else [self._values]
            out = []
            for r in rows:
                existing = next((x for x in self.t.rows if x.get("id") == r.get("id")), None)
                if existing is not None:
                    existing.update(r)
                    out.append(dict(existing))
                else:
                    out.append(self.t.insert_row(r))
            return _Result(out)

        matched = [r for r in self.t.rows if self._match(r)]

        if self._op == "update":
            for r in matched:
                r.update(self._values)
            return _Result([dict(r) for r in matched])

        if self._op == "delete":
            for r in list(matched):
                self.t.rows.remove(r)
            return _Result([dict(r) for r in matched])

        # select
        count = len(matched) if self._count else None
        if self._order:
            matched = sorted(
                matched, key=lambda r: r.get(self._order) or "", reverse=self._desc
            )
        if self._range:
            a, b = self._range
            matched = matched[a : b + 1]
        if self._limit is not None:
            matched = matched[: self._limit]
        return _Result([self._project(r) for r in matched], count=count)


class _Rpc:
    def __init__(self, db, fn, params):
        self.db, self.fn, self.params = db, fn, params

    def execute(self):
        users = self.db._table("users")
        uid = self.params["p_user_id"]
        amt = Decimal(str(self.params["p_amount"]))
        row = next((r for r in users.rows if r["id"] == uid), None)
        if self.fn == "deduct_balance":
            if row and Decimal(str(row["balance_usdc"])) >= amt:
                row["balance_usdc"] = float(Decimal(str(row["balance_usdc"])) - amt)
                return _Result(row["balance_usdc"])
            return _Result(None)
        if self.fn == "credit_balance":
            row["balance_usdc"] = float(Decimal(str(row["balance_usdc"])) + amt)
            return _Result(row["balance_usdc"])
        if self.fn == "credit_topup":
            # Mirror migrations/004_credit_topup.sql: insert the ledger row and
            # credit the balance atomically. The unique constraint on
            # solana_signature is the idempotency guard — a duplicate signature
            # credits nothing and returns NULL.
            txs = self.db._table("transactions")
            sig = self.params["p_signature"]
            if any(t.get("solana_signature") == sig for t in txs.rows):
                return _Result(None)
            txs.insert_row(
                {
                    "user_id": uid,
                    "type": "topup",
                    "amount": float(amt),
                    "token": "USDC",
                    "solana_signature": sig,
                    "status": "confirmed",
                    "metadata": {
                        "memo": self.params.get("p_memo"),
                        "intent_id": self.params.get("p_intent_id"),
                    },
                }
            )
            row["balance_usdc"] = float(Decimal(str(row["balance_usdc"])) + amt)
            return _Result(row["balance_usdc"])
        if self.fn == "credit_provider_earnings":
            row["available_usdc"] = float(Decimal(str(row.get("available_usdc", 0))) + amt)
            row["lifetime_earnings_usdc"] = float(
                Decimal(str(row.get("lifetime_earnings_usdc", 0))) + amt
            )
            return _Result(None)
        if self.fn == "lock_withdrawal":
            avail = Decimal(str(row.get("available_usdc", 0)))
            if avail < amt:
                return _Result(False)
            row["available_usdc"] = float(avail - amt)
            row["pending_withdrawal_usdc"] = float(
                Decimal(str(row.get("pending_withdrawal_usdc", 0))) + amt
            )
            return _Result(True)
        if self.fn == "settle_withdrawal":
            refund = self.params.get("p_refund", False)
            pending = Decimal(str(row.get("pending_withdrawal_usdc", 0)))
            row["pending_withdrawal_usdc"] = float(max(pending - amt, Decimal("0")))
            if refund:
                row["available_usdc"] = float(Decimal(str(row.get("available_usdc", 0))) + amt)
            return _Result(None)
        raise ValueError(f"Unknown rpc: {self.fn}")


class FakeSupabase:
    def __init__(self):
        self.tables: dict[str, FakeTable] = {}

    def _table(self, name) -> FakeTable:
        return self.tables.setdefault(name, FakeTable(name))

    def table(self, name) -> _Query:
        return _Query(self._table(name))

    def rpc(self, fn, params) -> _Rpc:
        return _Rpc(self, fn, params)

    # test helpers ----------------------------------------------------------
    def add_user(self, **fields) -> dict:
        defaults = {
            "id": str(uuid.uuid4()),
            "wallet_address": "wallet" + uuid.uuid4().hex[:8],
            "tier": "bronze",
            "balance_usdc": 1000.0,
            "is_active": True,
        }
        defaults.update(fields)
        return self._table("users").insert_row(defaults)
