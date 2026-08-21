"""Token intelligence: token/CA scans, wallet analysis, price, accumulation.

Everything runs against plain Solana JSON-RPC (via solana_service) plus the
Jupiter quote API. Design constraints that shape this module:

- getSignaturesForAddress on a MINT does not reveal transfers (transfers involve
  token accounts, not the mint), so mint-wide transfer history is impossible
  with plain RPC. Accumulation / large-transfer analysis is therefore anchored
  on TOKEN_WHALE_WATCHLIST_JSON (known wallets) and on holder snapshots cached
  in intel_scans.
- Plain RPC cannot enumerate a mint's holders; the only holder picture comes
  from snapshots stored by earlier scans or seeded by config.
- Liquidity is estimated only from pools listed in TOKEN_POOLS_JSON.

All lookups are fail-soft: a missing data source yields null/[] in the response,
never an error.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import httpx
from solders.pubkey import Pubkey
from supabase import Client

from app.config import settings
from app.logger import logger
from app.services import token_metadata
from app.services.solana_service import TOKEN_PROGRAM_ID, get_solana_service

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

_ACCUMULATION_WINDOW_DAYS = 7
# Net inflow that earns a full inflow_score: 2% of supply in the window.
_INFLOW_FULL_SCORE_RATIO = Decimal("0.02")
# Buy txs that earn a full activity_score.
_ACTIVITY_FULL_SCORE_TX = 20
# Top-10 share above which distribution score starts dropping; 50% is neutral.
_DISTRIBUTION_NEUTRAL_TOP10 = Decimal("0.50")

_scan_cache: dict[tuple[str, str], tuple[float, dict]] = {}


# --- cache ----------------------------------------------------------------

def _cache_get(scan_type: str, target: str) -> Optional[dict]:
    """Fresh module-level cache hit, else None."""
    ttl = settings.INTEL_SCAN_CACHE_TTL_SECONDS
    if ttl <= 0:
        return None
    entry = _scan_cache.get((scan_type, target))
    if entry is None:
        return None
    ts, payload = entry
    if time.monotonic() - ts < ttl:
        return payload
    return None


def _cache_put(scan_type: str, target: str, payload: dict) -> None:
    if settings.INTEL_SCAN_CACHE_TTL_SECONDS > 0:
        _scan_cache[(scan_type, target)] = (time.monotonic(), payload)


def reset_scan_cache() -> None:
    """Drop the in-memory cache (used by tests and admin ops)."""
    _scan_cache.clear()


def _db_cache_get(db: Client, scan_type: str, target: str) -> Optional[dict]:
    try:
        res = (
            db.table("intel_scans")
            .select("payload, updated_at")
            .eq("scan_type", scan_type)
            .eq("target", target)
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 — cache read is best-effort
        logger.warning("intel_scans read failed ({} {}): {}", scan_type, target, exc)
        return None
    if not res.data:
        return None
    row = res.data[0]
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None
    ts = row.get("updated_at")
    if not ts:
        return payload
    try:
        last = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return payload
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last).total_seconds()
    if age < settings.INTEL_SCAN_CACHE_TTL_SECONDS:
        return payload
    return None


def _db_cache_put(db: Client, scan_type: str, target: str, payload: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    row = {"scan_type": scan_type, "target": target, "payload": payload, "updated_at": now}
    try:
        db.table("intel_scans").upsert(row, on_conflict="scan_type,target").execute()
    except Exception as exc:  # noqa: BLE001 — cache write is best-effort
        logger.warning("intel_scans write failed ({} {}): {}", scan_type, target, exc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --- price -----------------------------------------------------------------

async def get_token_price_usdc(mint: str) -> Optional[Decimal]:
    """Jupiter quote mint -> USDC. Returns price per token, or None.

    Quotes 10^decimals of the mint (1 whole token) and divides the out amount by
    1e6 (USDC decimals). Fail-soft: any error returns None.
    """
    decimals = 9
    supply = None
    try:
        supply = await get_solana_service().get_token_supply(mint)
    except Exception:  # noqa: BLE001
        pass
    if supply and isinstance(supply.get("decimals"), int):
        decimals = supply["decimals"]

    amount = 10**decimals
    params = {
        "inputMint": mint,
        "outputMint": settings.USDC_MINT_ADDRESS or USDC_MINT,
        "amount": str(amount),
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{settings.JUPITER_QUOTE_API}/quote", params=params
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001 — price is best-effort
        logger.warning("Jupiter quote failed for {}: {}", mint, exc)
        return None
    out_amount = (data or {}).get("outAmount")
    if not out_amount:
        return None
    try:
        return Decimal(str(out_amount)) / Decimal(1_000_000)
    except Exception:  # noqa: BLE001
        return None


# --- token scan ------------------------------------------------------------

async def scan_token(db: Client, ca: str) -> dict:
    """Full token profile for a mint, cached in intel_scans."""
    cached = _cache_get("token", ca)
    if cached is not None:
        return cached
    cached = _db_cache_get(db, "token", ca)
    if cached is not None:
        _cache_put("token", ca, cached)
        return cached

    sol = get_solana_service()
    metadata = None
    supply_raw = None
    try:
        metadata = await token_metadata.fetch_metadata(sol, ca)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Metadata fetch failed for {}: {}", ca, exc)
    try:
        supply_raw = await sol.get_token_supply(ca)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supply fetch failed for {}: {}", ca, exc)

    price = await get_token_price_usdc(ca)

    liquidity_usdc, pool_count = await _estimate_liquidity(sol, ca)
    holders = await _load_holder_snapshot(db, ca)

    payload = {
        "mint": ca,
        "metadata": metadata,
        "supply": supply_raw,
        "price_usdc": float(price) if price is not None else None,
        "liquidity": {"estimated_usdc": liquidity_usdc, "pool_count": pool_count},
        "holders": holders,
        "risk": _build_risk(metadata, liquidity_usdc, holders),
        "scanned_at": _now_iso(),
    }
    _cache_put("token", ca, payload)
    _db_cache_put(db, "token", ca, payload)
    return payload


async def _estimate_liquidity(sol, mint: str) -> tuple[Optional[float], int]:
    """Sum UI balances of configured pool token accounts for the mint."""
    pools = settings.token_pools.get(mint, [])
    if not pools:
        return None, 0
    total = Decimal(0)
    for pool_ata in pools:
        try:
            total += await sol.get_token_balance(pool_ata, mint)
        except Exception as exc:  # noqa: BLE001 — per-pool failure is not fatal
            logger.warning("Pool balance failed for {} ({}): {}", mint, pool_ata, exc)
    return float(total), len(pools)


async def _load_holder_snapshot(db: Client, mint: str) -> Optional[dict]:
    """Holder snapshot from a previously cached token scan (if one exists)."""
    cached = _cache_get("token", mint)
    if cached is None:
        cached = _db_cache_get(db, "token", mint)
    if cached and cached.get("holders"):
        return cached["holders"]
    return None


async def refresh_holder_snapshot(db: Client, mint: str) -> dict:
    """Rebuild the holder snapshot for a mint from the configured watchlist.

    Plain Solana RPC cannot enumerate a mint's holders, so the snapshot is
    derived from TOKEN_WHALE_WATCHLIST_JSON: each watchlist wallet's balance of
    the mint is read and stored as a top-holder entry. The result is merged into
    the cached token scan so scans/accumulation pick it up immediately.

    Returns the snapshot dict. Raises ValidationError when the watchlist is
    empty or the mint is invalid.
    """
    from app.exceptions import ValidationError

    watchlist = settings.token_whale_watchlist
    if not watchlist:
        raise ValidationError(
            "TOKEN_WHALE_WATCHLIST_JSON is empty — configure it to seed holder snapshots"
        )
    try:
        Pubkey.from_string(mint)
    except Exception as exc:  # noqa: BLE001
        raise ValidationError("Invalid Solana mint address") from exc

    sol = get_solana_service()
    balances: list[tuple[str, float]] = []
    for wallet in watchlist:
        try:
            balance = await sol.get_token_balance(wallet, mint)
            balances.append((wallet, float(balance)))
        except Exception as exc:  # noqa: BLE001 — per-wallet failure is not fatal
            logger.warning("Holder-snapshot balance failed for {} ({}): {}", mint, wallet, exc)

    # Ranked top holders (only non-zero balances), top-10 share if supply is known.
    holders = sorted(
        (h for h in balances if h[1] > 0), key=lambda h: h[1], reverse=True
    )
    top10_share = None
    supply_raw = None
    try:
        supply_raw = await sol.get_token_supply(mint)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Holder-snapshot supply failed for {}: {}", mint, exc)
    if supply_raw and supply_raw.get("uiAmountString"):
        try:
            supply = Decimal(str(supply_raw["uiAmountString"]))
            if supply > 0:
                top10_share = float(
                    sum(Decimal(str(b)) for _w, b in holders[:10]) / supply
                )
        except Exception:  # noqa: BLE001
            top10_share = None

    snapshot = {
        "total_holders": len(holders),
        "top_holders": [
            {"wallet": w, "balance": b} for w, b in holders
        ],
        "top10_share": top10_share,
        "as_of": _now_iso(),
    }

    # Merge into the cached token scan so scan_token / accumulation see it.
    cached = _cache_get("token", mint)
    if cached is None:
        cached = _db_cache_get(db, "token", mint)
    if cached and isinstance(cached, dict):
        cached["holders"] = snapshot
        cached["scanned_at"] = _now_iso()
    else:
        # No prior scan: seed a minimal token-scan payload holding the snapshot.
        cached = {
            "mint": mint,
            "metadata": None,
            "supply": supply_raw,
            "price_usdc": None,
            "liquidity": {"estimated_usdc": None, "pool_count": 0},
            "holders": snapshot,
            "risk": {"warnings": []},
            "scanned_at": _now_iso(),
        }
    _cache_put("token", mint, cached)
    _db_cache_put(db, "token", mint, cached)
    return snapshot


def _build_risk(
    metadata: Optional[dict], liquidity_usdc: Optional[float], holders: Optional[dict]
) -> dict:
    warnings: list[str] = []
    if not metadata or not metadata.get("name"):
        warnings.append("No on-chain token metadata — the mint may be unaudited or unverified")
    if liquidity_usdc is None:
        warnings.append("No liquidity pools configured — liquidity is unknown")
    if holders and holders.get("top10_share") is not None:
        try:
            top10 = Decimal(str(holders["top10_share"]))
        except Exception:  # noqa: BLE001
            top10 = Decimal(0)
        if top10 > Decimal("0.80"):
            warnings.append("High concentration — the top 10 holders control >80% of supply")
    return {"warnings": warnings}


# --- wallet analysis -------------------------------------------------------

async def analyze_wallet(db: Client, wallet: str, mint: Optional[str] = None) -> dict:
    """Wallet profile: holdings, recent activity, optional per-mint buy history."""
    cached = _cache_get("wallet", wallet + (f":{mint}" if mint else ""))
    if cached is not None:
        return cached

    sol = get_solana_service()
    holdings, symbols = await _load_holdings(sol, wallet)
    activity = await _load_activity(sol, wallet)
    buy_history = []
    if mint:
        buy_history = await _load_buy_history(sol, wallet, mint)

    payload = {
        "wallet": wallet,
        "holdings": holdings,
        "recent_activity": activity,
        "buy_history": buy_history,
        "analyzed_at": _now_iso(),
    }
    _cache_put("wallet", wallet + (f":{mint}" if mint else ""), payload)
    return payload


async def _load_holdings(sol, wallet: str) -> tuple[list[dict], dict[str, str]]:
    """Token accounts the wallet owns, capped, with optional metadata for a few."""
    try:
        accounts = await sol.get_token_accounts_by_owner(wallet, settings.USDC_MINT_ADDRESS or "")
    except Exception:  # noqa: BLE001
        accounts = []
    # getTokenAccountsByOwner requires a mint filter; when the USDC mint is not
    # configured we fall back to the token program so all holdings come back.
    try:
        from app.services.solana_service import TOKEN_PROGRAM_ID

        if not accounts:
            result = await sol._rpc(
                "getTokenAccountsByOwner",
                [wallet, {"programId": TOKEN_PROGRAM_ID}, {"encoding": "jsonParsed"}],
            )
            accounts = (result or {}).get("value", []) or []
    except Exception as exc:  # noqa: BLE001 — holdings are best-effort
        logger.warning("Holdings fetch failed for {}: {}", wallet, exc)

    holdings: list[dict] = []
    symbols: dict[str, str] = {}
    for acc in accounts[: settings.MAX_TOKEN_ACCOUNTS_PER_WALLET]:
        try:
            info = acc["account"]["data"]["parsed"]["info"]
            mint_addr = info["mint"]
            amount = float((info.get("tokenAmount") or {}).get("uiAmount") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        holdings.append({"mint": mint_addr, "ui_amount": amount, "symbol": None, "name": None})
    return holdings, symbols


async def _load_activity(sol, wallet: str) -> list[dict]:
    """Recent wallet signatures parsed into transfer/memo entries (capped)."""
    try:
        sigs = await sol.get_signatures_for_address(wallet, limit=25)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Signature fetch failed for {}: {}", wallet, exc)
        return []
    out: list[dict] = []
    for entry in sigs[: settings.MAX_WALLET_TX_PARSE]:
        signature = entry.get("signature")
        if not signature:
            continue
        try:
            parsed = await sol.get_parsed_transaction(signature)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tx parse failed for {}: {}", signature, exc)
            continue
        if not parsed:
            continue
        memo = sol.extract_memo(parsed)
        transfers = _transfers_involving_wallet(parsed, wallet)
        out.append(
            {
                "signature": signature,
                "slot": entry.get("slot"),
                "timestamp": entry.get("blockTime"),
                "memo": memo,
                "transfers": transfers[:20],
            }
        )
    return out


def _transfers_involving_wallet(parsed: dict, wallet: str) -> list[dict]:
    """SPL transfers in a tx where `wallet` is the source or destination."""
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
        source = info.get("source")
        dest = info.get("destination")
        if source != wallet and dest != wallet:
            continue
        token_amount = info.get("tokenAmount") or {}
        amount_str = token_amount.get("uiAmountString") or str(token_amount.get("uiAmount"))
        out.append(
            {
                "mint": info.get("mint"),
                "ui_amount": float(amount_str) if amount_str else 0.0,
                "source": source,
                "destination": dest,
            }
        )
    return out


async def _load_buy_history(sol, wallet: str, mint: str) -> list[dict]:
    """Parsed inflow transfers of `mint` into `wallet` (recent signatures)."""
    try:
        sigs = await sol.get_signatures_for_address(wallet, limit=100)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Buy-history signature fetch failed for {}: {}", wallet, exc)
        return []
    out: list[dict] = []
    for entry in sigs[: settings.MAX_WALLET_TX_PARSE * 5]:
        signature = entry.get("signature")
        if not signature:
            continue
        try:
            parsed = await sol.get_parsed_transaction(signature)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Buy-history tx parse failed for {}: {}", signature, exc)
            continue
        if not parsed:
            continue
        for t in _transfers_involving_wallet(parsed, wallet):
            if t.get("mint") != mint or t.get("destination") != wallet:
                continue
            out.append(
                {
                    "signature": signature,
                    "amount": t.get("ui_amount", 0.0),
                    "timestamp": entry.get("blockTime"),
                    "counterparty": t.get("source"),
                }
            )
            break
        if len(out) >= 20:
            break
    return out


# --- accumulation ----------------------------------------------------------

async def compute_accumulation(db: Client, ca: str, *, bypass_cache: bool = False) -> dict:
    """Accumulation score for a mint, from watchlist wallets + cached snapshot."""
    if not bypass_cache:
        cached = _cache_get("accumulation", ca)
        if cached is not None:
            return cached
        cached = _db_cache_get(db, "accumulation", ca)
        if cached is not None:
            _cache_put("accumulation", ca, cached)
            return cached

    watchlist = settings.token_whale_watchlist
    inflow, buy_tx = await _watchlist_flows(ca, watchlist)

    supply_raw = None
    try:
        supply_raw = await get_solana_service().get_token_supply(ca)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supply fetch failed for {}: {}", ca, exc)

    supply = None
    if supply_raw and supply_raw.get("uiAmountString"):
        try:
            supply = Decimal(str(supply_raw["uiAmountString"]))
        except Exception:  # noqa: BLE001
            supply = None

    top10_share = None
    snapshot = await _load_holder_snapshot(db, ca)
    if snapshot:
        top10_share = snapshot.get("top10_share")

    metrics = {
        "watchlist_wallets": len(watchlist),
        "inflow_7d": float(inflow),
        "inflow_ratio": float(inflow / supply) if supply and supply > 0 else None,
        "buy_tx_7d": buy_tx,
        "top10_share": float(top10_share) if top10_share is not None else None,
    }
    score, label = _accumulation_score(metrics)
    metrics["inflow_score"] = _inflow_score(Decimal(str(metrics["inflow_7d"])), supply)
    metrics["activity_score"] = _activity_score(buy_tx)
    metrics["distribution_score"] = _distribution_score(
        Decimal(str(top10_share)) if top10_share is not None else None
    )

    payload = {
        "mint": ca,
        "score": score,
        "label": label,
        "metrics": metrics,
        "computed_at": _now_iso(),
    }
    if not bypass_cache:
        _cache_put("accumulation", ca, payload)
        _db_cache_put(db, "accumulation", ca, payload)
    return payload


async def _watchlist_flows(mint: str, watchlist: list[str]) -> tuple[Decimal, int]:
    """Net inflow (UI) + distinct buy-transfer count for `mint` across watchlist.

    Anchored on the watchlist because mint addresses never appear in ordinary
    transfer transactions — only the wallets involved do.
    """
    if not watchlist:
        return Decimal(0), 0
    since = _utc_now() - timedelta(days=_ACCUMULATION_WINDOW_DAYS)
    since_ts = int(since.timestamp())
    sol = get_solana_service()

    net = Decimal(0)
    buy_txs: set[str] = set()
    for wallet in watchlist:
        try:
            sigs = await sol.get_signatures_for_address(wallet, limit=100)
        except Exception as exc:  # noqa: BLE001 — per-wallet failure is not fatal
            logger.warning("Watchlist signature fetch failed for {}: {}", wallet, exc)
            continue
        for entry in sigs:
            block_time = entry.get("blockTime")
            if block_time is None or int(block_time) < since_ts:
                break  # signatures are newest-first; past the window, stop
            signature = entry.get("signature")
            if entry.get("err") is not None or not signature:
                continue
            try:
                parsed = await sol.get_parsed_transaction(signature)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Watchlist tx parse failed for {}: {}", signature, exc)
                continue
            if not parsed:
                continue
            for t in _transfers_of_mint(parsed, mint, wallet):
                amount = Decimal(str(t["ui_amount"]))
                net += amount
                if t["direction"] == "in":
                    buy_txs.add(signature)
    return net, len(buy_txs)


def _transfers_of_mint(parsed: dict, mint: str, wallet: str) -> list[dict]:
    """Transfers of `mint` where `wallet` is source or destination, with direction."""
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
        direction = "in" if dest == wallet else "out"
        out.append({"ui_amount": amount, "direction": direction, "source": source, "destination": dest})
    return out


def _clamp01(value: Decimal) -> Decimal:
    return max(Decimal(0), min(Decimal(1), value))


def _inflow_score(inflow: Decimal, supply: Optional[Decimal]) -> float:
    if supply is None or supply <= 0:
        return 0.0
    ratio = inflow / supply
    return float(_clamp01(ratio / _INFLOW_FULL_SCORE_RATIO) * 100)


def _activity_score(buy_tx: int) -> float:
    return float(_clamp01(Decimal(buy_tx) / Decimal(_ACTIVITY_FULL_SCORE_TX)) * 100)


def _distribution_score(top10_share: Optional[Decimal]) -> float:
    """50 when no snapshot; 100 when top10 holds <=50%; 0 when top10 holds 100%."""
    if top10_share is None:
        return 50.0
    excess = max(top10_share - _DISTRIBUTION_NEUTRAL_TOP10, Decimal(0))
    return float(_clamp01(Decimal(1) - excess / Decimal("0.5")) * 100)


def _accumulation_score(metrics: dict) -> tuple[int, str]:
    """Combine metrics into a 0-100 score + label. Pure, unit-testable.

    score = 0.5*inflow + 0.3*activity + 0.2*distribution
    label = "distribution" when net inflow < 0, else strong/moderate/weak.
    """
    inflow = Decimal(str(metrics.get("inflow_7d", 0) or 0))
    supply = None
    if metrics.get("inflow_ratio") is not None:
        ratio = Decimal(str(metrics["inflow_ratio"]))
        if ratio > 0:
            supply = inflow / ratio
    inflow_score = _inflow_score(inflow, supply)
    activity_score = _activity_score(int(metrics.get("buy_tx_7d", 0) or 0))
    top10 = metrics.get("top10_share")
    distribution_score = _distribution_score(
        Decimal(str(top10)) if top10 is not None else None
    )
    score = round(
        0.5 * inflow_score + 0.3 * activity_score + 0.2 * distribution_score
    )
    if inflow < 0:
        label = "distribution"
    elif score >= 70:
        label = "strong"
    elif score >= 40:
        label = "moderate"
    else:
        label = "weak"
    return score, label
