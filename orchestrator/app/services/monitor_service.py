"""Monitoring agents: background evaluation of user monitors + webhook delivery.

Two jobs in one asyncio worker (ENABLE_MONITOR_WORKER):

1. process_due_monitors() — for each active monitor past its interval, evaluate
   its conditions against fresh token/wallet data and write alert_events rows.
   Dedup is select-then-insert keyed on (monitor_id, dedup_key), with the DB
   unique index as a race backstop. Per-monitor last_cursor tracks how far into
   on-chain signature history the activity conditions have already read, so a
   signature is alerted once.

2. dispatch_webhooks() — drains the alert_webhooks outbox with exponential
   backoff: attempt n waits 2^(n-1) * WEBHOOK_RETRY_BASE_SECONDS, and a delivery
   is marked failed after WEBHOOK_MAX_ATTEMPTS.

Condition types (per target_type):
    token:   {"type": "accumulation_score", "gte": N}
             {"type": "price_drop_pct", "gte": P}
             {"type": "large_transfer", "min_ui_amount": A}
    wallet:  {"type": "new_activity"}
             {"type": "large_inflow", "min_ui_amount": A}

The worker is fail-soft: one monitor or one webhook failing never aborts the
cycle.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import httpx
from supabase import Client

from app.config import settings
from app.database import get_supabase
from app.logger import logger
from app.services import token_intel
from app.services.solana_service import get_solana_service

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _is_due(monitor: dict) -> bool:
    last = _parse_ts(monitor.get("last_checked_at"))
    if last is None:
        return True
    interval = int(monitor.get("interval_minutes") or settings.MONITOR_DEFAULT_INTERVAL_MINUTES)
    return (_utc_now() - last).total_seconds() >= interval * 60


class MonitorService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="monitor-worker")
        logger.info(
            "Monitor worker started (interval={}s, max_per_cycle={})",
            settings.MONITOR_WORKER_INTERVAL_SECONDS,
            settings.MONITOR_MAX_PER_CYCLE,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(
                    self._task, timeout=settings.MONITOR_WORKER_INTERVAL_SECONDS + 5
                )
            except asyncio.TimeoutError:
                self._task.cancel()
        logger.info("Monitor worker stopped")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.process_due_monitors()
            except Exception as exc:  # noqa: BLE001 — never crash the loop
                logger.error("Monitor evaluation cycle failed (will retry): {}", exc)
            try:
                await self.refresh_holder_snapshots()
            except Exception as exc:  # noqa: BLE001 — never crash the loop
                logger.error("Holder-snapshot refresh cycle failed (will retry): {}", exc)
            try:
                await self.dispatch_webhooks()
            except Exception as exc:  # noqa: BLE001 — never crash the loop
                logger.error("Webhook dispatch cycle failed (will retry): {}", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=settings.MONITOR_WORKER_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass  # normal: interval elapsed, run again

    # --- holder snapshot refresh -------------------------------------------
    async def refresh_holder_snapshots(self) -> None:
        """Refresh holder snapshots for monitored tokens on a schedule.

        Accumulation scoring needs a holder snapshot (top10_share); without
        refresh the snapshot only exists when an admin runs
        POST /v1/admin/intel/holder-snapshot. This makes monitored tokens
        self-maintaining, throttled by INTEL_HOLDER_SNAPSHOT_TTL_SECONDS via
        the intel_scans cache (refresh_holder_snapshot merges into it and the
        DB-cache TTL guards how often the RPC work repeats).
        """
        ttl = settings.INTEL_HOLDER_SNAPSHOT_TTL_SECONDS
        if ttl <= 0 or not settings.token_whale_watchlist:
            return  # automatic refresh disabled, or nothing to derive holders from
        db = get_supabase()
        res = (
            db.table("monitors")
            .select("target_address")
            .eq("is_active", True)
            .eq("target_type", "token")
            .execute()
        )
        for monitor in res.data or []:
            mint = monitor.get("target_address")
            if not mint:
                continue
            try:
                await token_intel.refresh_holder_snapshot(db, mint)
            except Exception as exc:  # noqa: BLE001 — one token is not fatal
                logger.warning(
                    "Holder-snapshot refresh failed for {}: {}", mint, exc
                )

    # --- monitor evaluation ------------------------------------------------
    async def process_due_monitors(self) -> None:
        db = get_supabase()
        res = (
            db.table("monitors")
            .select("*")
            .eq("is_active", True)
            .order("last_checked_at", desc=False)  # nulls first → oldest first
            .limit(settings.MONITOR_MAX_PER_CYCLE)
            .execute()
        )
        for monitor in res.data or []:
            if not _is_due(monitor):
                continue
            try:
                await self._evaluate_monitor(db, monitor)
            except Exception as exc:  # noqa: BLE001 — one bad monitor is not fatal
                logger.error(
                    "Monitor {} evaluation failed: {}",
                    monitor.get("id"),
                    exc,
                )
            finally:
                try:
                    db.table("monitors").update(
                        {"last_checked_at": _now_iso()}
                    ).eq("id", monitor["id"]).execute()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not update last_checked_at for {}: {}", monitor.get("id"), exc)

    async def _evaluate_monitor(self, db: Client, monitor: dict) -> None:
        target_type = monitor.get("target_type")
        conditions = monitor.get("conditions") or []
        if not isinstance(conditions, list):
            conditions = []

        for cond in conditions:
            if not isinstance(cond, dict):
                continue
            ctype = cond.get("type")
            try:
                if target_type == "token":
                    await self._eval_token_condition(db, monitor, cond, ctype)
                elif target_type == "wallet":
                    await self._eval_wallet_condition(db, monitor, cond, ctype)
            except Exception as exc:  # noqa: BLE001 — one condition is not fatal
                logger.error(
                    "Monitor {} condition {} failed: {}",
                    monitor.get("id"),
                    ctype,
                    exc,
                )

    # --- token conditions --------------------------------------------------
    async def _eval_token_condition(self, db: Client, monitor: dict, cond: dict, ctype: str) -> None:
        mint = monitor["target_address"]
        if ctype == "accumulation_score":
            await self._eval_accumulation(db, monitor, cond)
        elif ctype == "price_drop_pct":
            await self._eval_price_drop(db, monitor, cond)
        elif ctype == "large_transfer":
            await self._eval_large_transfer(db, monitor, cond, mint)
        else:
            logger.warning("Unknown token condition type for monitor {}: {}", monitor.get("id"), ctype)

    async def _eval_accumulation(self, db: Client, monitor: dict, cond: dict) -> None:
        threshold = float(cond.get("gte") or 0)
        result = await token_intel.compute_accumulation(db, monitor["target_address"], bypass_cache=True)
        score = int(result.get("score", 0))
        if score < threshold:
            return
        dedup_key = f"acc:{_utc_now().strftime('%Y-%m-%d')}"
        message = (
            f"Accumulation score {score} for {monitor['target_address']} "
            f"(threshold {threshold:g})"
        )
        await self._emit_alert(db, monitor, "accumulation_score", message, dedup_key, {"score": score, "label": result.get("label")})

    async def _eval_price_drop(self, db: Client, monitor: dict, cond: dict) -> None:
        threshold = float(cond.get("gte") or 0)
        baseline = monitor.get("baseline_price_usdc")
        current = await token_intel.get_token_price_usdc(monitor["target_address"])
        if current is None:
            return  # no price data — cannot evaluate
        current_f = float(current)
        if baseline is None or float(baseline) <= 0:
            # First evaluation: adopt the current price as the baseline and skip.
            db.table("monitors").update(
                {"baseline_price_usdc": current_f}
            ).eq("id", monitor["id"]).execute()
            return
        baseline_f = float(baseline)
        drop_pct = (baseline_f - current_f) / baseline_f * 100
        if drop_pct < threshold:
            return
        dedup_key = f"price:{_utc_now().strftime('%Y-%m-%d')}"
        message = (
            f"Price dropped {drop_pct:.1f}% for {monitor['target_address']} "
            f"(baseline ${baseline_f:.6g} -> now ${current_f:.6g})"
        )
        await self._emit_alert(
            db,
            monitor,
            "price_drop_pct",
            message,
            dedup_key,
            {"drop_pct": round(drop_pct, 2), "baseline_price_usdc": baseline_f, "current_price_usdc": current_f},
        )

    async def _eval_large_transfer(self, db: Client, monitor: dict, cond: dict, mint: str) -> None:
        """Scan watchlist wallets for new transfers of the token >= the threshold."""
        min_amount = Decimal(str(cond.get("min_ui_amount") or 0))
        watchlist = settings.token_whale_watchlist
        if not watchlist:
            return  # nothing to watch without a watchlist
        cursor = monitor.get("last_cursor")
        sol = get_solana_service()
        newest: Optional[str] = None

        for wallet in watchlist:
            try:
                sigs = await sol.get_signatures_for_address(wallet, limit=100, until=cursor)
            except Exception as exc:  # noqa: BLE001 — per-wallet failure is not fatal
                logger.warning("Large-transfer sig fetch failed for {}: {}", wallet, exc)
                continue
            for entry in reversed(sigs):  # oldest-first so the cursor advances monotonically
                signature = entry.get("signature")
                if not signature or entry.get("err") is not None:
                    continue
                if newest is None:
                    newest = signature
                try:
                    parsed = await sol.get_parsed_transaction(signature)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Large-transfer tx parse failed for {}: {}", signature, exc)
                    continue
                if not parsed:
                    continue
                for t in _transfers_of_mint(parsed, mint, wallet):
                    if t["direction"] != "in" or t["amount"] < min_amount:
                        continue
                    await self._emit_alert(
                        db,
                        monitor,
                        "large_transfer",
                        f"Large transfer: {t['amount']:.6g} {mint} into {wallet}",
                        f"tx:{signature}",
                        {
                            "signature": signature,
                            "wallet": wallet,
                            "amount": float(t["amount"]),
                            "mint": mint,
                        },
                    )

        if newest is not None:
            db.table("monitors").update({"last_cursor": newest}).eq("id", monitor["id"]).execute()

    # --- wallet conditions -------------------------------------------------
    async def _eval_wallet_condition(self, db: Client, monitor: dict, cond: dict, ctype: str) -> None:
        wallet = monitor["target_address"]
        if ctype == "new_activity":
            await self._eval_new_activity(db, monitor, wallet)
        elif ctype == "large_inflow":
            await self._eval_large_inflow(db, monitor, cond, wallet)
        else:
            logger.warning("Unknown wallet condition type for monitor {}: {}", monitor.get("id"), ctype)

    async def _eval_new_activity(self, db: Client, monitor: dict, wallet: str) -> None:
        cursor = monitor.get("last_cursor")
        sol = get_solana_service()
        try:
            sigs = await sol.get_signatures_for_address(wallet, limit=25, until=cursor)
        except Exception as exc:  # noqa: BLE001
            logger.warning("New-activity sig fetch failed for {}: {}", wallet, exc)
            return
        if not sigs:
            return
        first = sigs[0]["signature"]
        if cursor and first == cursor:
            return  # nothing new
        await self._emit_alert(
            db,
            monitor,
            "new_activity",
            f"{len(sigs)} new transaction(s) for {wallet}",
            f"sig:{first}",
            {"wallet": wallet, "new_transactions": len(sigs), "newest_signature": first},
        )
        db.table("monitors").update({"last_cursor": first}).eq("id", monitor["id"]).execute()

    async def _eval_large_inflow(self, db: Client, monitor: dict, cond: dict, wallet: str) -> None:
        min_amount = Decimal(str(cond.get("min_ui_amount") or 0))
        cursor = monitor.get("last_cursor")
        sol = get_solana_service()
        try:
            sigs = await sol.get_signatures_for_address(wallet, limit=25, until=cursor)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Large-inflow sig fetch failed for {}: {}", wallet, exc)
            return
        newest: Optional[str] = None
        for entry in reversed(sigs):
            signature = entry.get("signature")
            if not signature or entry.get("err") is not None:
                continue
            if newest is None:
                newest = signature
            try:
                parsed = await sol.get_parsed_transaction(signature)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Large-inflow tx parse failed for {}: {}", signature, exc)
                continue
            if not parsed:
                continue
            for t in _transfers_of_any_mint(parsed, wallet):
                if t["direction"] != "in" or t["amount"] < min_amount:
                    continue
                await self._emit_alert(
                    db,
                    monitor,
                    "large_inflow",
                    f"Inflow of {t['amount']:.6g} {t['mint']} into {wallet}",
                    f"tx:{signature}",
                    {"signature": signature, "mint": t["mint"], "amount": float(t["amount"]), "wallet": wallet},
                )
        if newest is not None:
            db.table("monitors").update({"last_cursor": newest}).eq("id", monitor["id"]).execute()

    # --- alert emission + outbox -------------------------------------------
    async def _emit_alert(
        self, db: Client, monitor: dict, condition_type: str, message: str, dedup_key: str, payload: dict
    ) -> None:
        monitor_id = monitor["id"]
        # Select-then-insert: the unique (monitor_id, dedup_key) index is the backstop.
        existing = (
            db.table("alert_events")
            .select("id")
            .eq("monitor_id", monitor_id)
            .eq("dedup_key", dedup_key)
            .limit(1)
            .execute()
        )
        if existing.data:
            logger.info("Alert already emitted for monitor {} key {} — skipping", monitor_id, dedup_key)
            return

        row = {
            "monitor_id": monitor_id,
            "user_id": monitor.get("user_id"),
            "condition_type": condition_type,
            "message": message,
            "payload": payload,
            "dedup_key": dedup_key,
        }
        inserted = db.table("alert_events").insert(row).execute().data[0]
        logger.info("Alert emitted for monitor {} ({}): {}", monitor_id, condition_type, message)

        webhook_url = monitor.get("webhook_url")
        if not webhook_url:
            return
        delivery_payload = {
            "event_id": str(inserted["id"]),
            "monitor_id": str(monitor_id),
            "condition_type": condition_type,
            "message": message,
            "payload": payload,
            "occurred_at": inserted.get("occurred_at") or _now_iso(),
        }
        try:
            db.table("alert_webhooks").insert(
                {
                    "alert_event_id": str(inserted["id"]),
                    "monitor_id": str(monitor_id),
                    "webhook_url": webhook_url,
                    "payload": delivery_payload,
                    "status": "pending",
                    "attempts": 0,
                    "next_retry_at": None,
                }
            ).execute()
        except Exception as exc:  # noqa: BLE001 — outbox write is best-effort
            logger.warning("Could not enqueue webhook for alert {}: {}", inserted.get("id"), exc)

    # --- webhook delivery --------------------------------------------------
    async def dispatch_webhooks(self) -> None:
        db = get_supabase()
        res = (
            db.table("alert_webhooks")
            .select("*")
            .in_("status", ["pending", "sending"])
            .execute()
        )
        for row in res.data or []:
            next_retry = _parse_ts(row.get("next_retry_at"))
            if next_retry is not None and next_retry > _utc_now():
                continue
            try:
                await self._deliver_webhook(db, row)
            except Exception as exc:  # noqa: BLE001 — one delivery is not fatal
                logger.error("Webhook {} delivery failed: {}", row.get("id"), exc)

    async def _deliver_webhook(self, db: Client, row: dict) -> None:
        wid = row["id"]
        db.table("alert_webhooks").update(
            {"status": "sending", "updated_at": _now_iso()}
        ).eq("id", wid).execute()

        attempts = int(row.get("attempts") or 0)
        url = row.get("webhook_url")
        payload = row.get("payload") or {}
        ok = False
        error = ""
        try:
            async with httpx.AsyncClient(timeout=settings.WEBHOOK_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=payload, timeout=settings.WEBHOOK_TIMEOUT_SECONDS)
            ok = 200 <= resp.status_code < 300
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            logger.warning("Webhook {} POST to {} failed: {}", wid, url, exc)

        if ok:
            db.table("alert_webhooks").update(
                {"status": "delivered", "attempts": attempts + 1, "updated_at": _now_iso()}
            ).eq("id", wid).execute()
            logger.info("Webhook {} delivered (attempt {})", wid, attempts + 1)
            return

        attempts += 1
        if attempts >= settings.WEBHOOK_MAX_ATTEMPTS:
            db.table("alert_webhooks").update(
                {
                    "status": "failed",
                    "attempts": attempts,
                    "last_error": error,
                    "updated_at": _now_iso(),
                }
            ).eq("id", wid).execute()
            logger.error("Webhook {} failed permanently after {} attempts", wid, attempts)
            return

        backoff = settings.WEBHOOK_RETRY_BASE_SECONDS * (2 ** (attempts - 1))
        next_retry = (_utc_now() + timedelta(seconds=backoff)).isoformat()
        db.table("alert_webhooks").update(
            {
                "status": "pending",
                "attempts": attempts,
                "last_error": error,
                "next_retry_at": next_retry,
                "updated_at": _now_iso(),
            }
        ).eq("id", wid).execute()
        logger.info("Webhook {} retry in {}s (attempt {})", wid, backoff, attempts)


# --- transfer parsing helpers ----------------------------------------------

def _transfers_of_mint(parsed: dict, mint: str, wallet: str) -> list[dict]:
    """Transfers of `mint` where `wallet` is source or destination."""
    out: list[dict] = []
    try:
        instrs = parsed["transaction"]["message"]["instructions"]
    except (KeyError, TypeError):
        return out
    for ix in instrs:
        if ix.get("program") != "spl-token" and ix.get("programId") != TOKEN_PROGRAM_ID:
            continue
        parsed_ix = ix.get("parsed") or {}
        if parsed_ix.get("type") not in ("transfer", "transferChecked"):
            continue
        info = parsed_ix.get("info", {})
        if info.get("mint") != mint:
            continue
        source, dest = info.get("source"), info.get("destination")
        if source != wallet and dest != wallet:
            continue
        token_amount = info.get("tokenAmount") or {}
        amount_str = token_amount.get("uiAmountString") or str(token_amount.get("uiAmount"))
        try:
            amount = Decimal(str(amount_str))
        except Exception:  # noqa: BLE001
            continue
        out.append(
            {
                "amount": amount,
                "direction": "in" if dest == wallet else "out",
                "source": source,
                "destination": dest,
            }
        )
    return out


def _transfers_of_any_mint(parsed: dict, wallet: str) -> list[dict]:
    """Any-mint transfers where `wallet` is source or destination."""
    out: list[dict] = []
    try:
        instrs = parsed["transaction"]["message"]["instructions"]
    except (KeyError, TypeError):
        return out
    for ix in instrs:
        if ix.get("program") != "spl-token" and ix.get("programId") != TOKEN_PROGRAM_ID:
            continue
        parsed_ix = ix.get("parsed") or {}
        if parsed_ix.get("type") not in ("transfer", "transferChecked"):
            continue
        info = parsed_ix.get("info", {})
        source, dest = info.get("source"), info.get("destination")
        if source != wallet and dest != wallet:
            continue
        token_amount = info.get("tokenAmount") or {}
        amount_str = token_amount.get("uiAmountString") or str(token_amount.get("uiAmount"))
        try:
            amount = Decimal(str(amount_str))
        except Exception:  # noqa: BLE001
            continue
        out.append(
            {
                "amount": amount,
                "direction": "in" if dest == wallet else "out",
                "mint": info.get("mint"),
                "source": source,
                "destination": dest,
            }
        )
    return out


monitor_service = MonitorService()
