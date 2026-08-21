"""Real top-holder enumeration + early-buyer tracing via plain Solana RPC.

Plain RPC *can* do better than a configured watchlist:
  - getTokenLargestAccounts(mint) returns the ~20 largest token accounts,
  - getAccountInfo(jsonParsed) resolves each account to its owner wallet,
  - getSignaturesForAddress(owner) + parsed transfers reveal each holder's
    first purchase of the mint.

That gives a real holder distribution (top10_share) and an "early buyer"
picture (who bought first, and how much) without any third-party analytics.
Everything is fail-soft: an RPC error yields what was already gathered, never
an exception. Results are cached in intel_scans so scans stay cheap.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from decimal import Decimal
from typing import Optional

from supabase import Client

from app.logger import logger
from app.services import token_intel
from app.services.solana_service import get_solana_service

# How many top holders to resolve owners for, and how many buy txs to page back.
MAX_TOP_HOLDERS = 20
EARLY_BUYER_SCAN_SIGNATURES = 200

# Clustering constants
_FUNDING_SCAN_SIGNATURES = 50
_HOLDING_SCAN_LIMIT = 50
_COORDINATED_TIMING_WINDOW_SECONDS = 60
_HOLDINGS_JACCARD_THRESHOLD = 0.5


async def top_holders(db: Client, mint: str, *, bypass_cache: bool = False) -> dict:
    """Ranked real top holders of a mint, resolved to owner wallets.

    Returns {"total_holders", "top_holders": [{wallet, token_account, balance}],
    "top10_share", "as_of"}. Falls back to the watchlist-derived snapshot when
    getTokenLargestAccounts yields nothing (e.g. an unlisted mint).
    """
    if not bypass_cache:
        cached = token_intel._cache_get("top_holders", mint)
        if cached is not None:
            return cached
        cached = token_intel._db_cache_get(db, "top_holders", mint)
        if cached is not None:
            token_intel._cache_put("top_holders", mint, cached)
            return cached

    sol = get_solana_service()
    try:
        accounts = await sol.get_token_largest_accounts(mint)
    except Exception as exc:  # noqa: BLE001
        logger.warning("getTokenLargestAccounts failed for {}: {}", mint, exc)
        accounts = []

    holders: list[dict] = []
    for acc in accounts[:MAX_TOP_HOLDERS]:
        address = acc.get("address")
        if not address:
            continue
        try:
            owner = await sol.get_token_account_owner(address)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Owner resolve failed for {} ({}): {}", mint, address, exc)
            continue
        if not owner:
            continue
        try:
            balance = float(acc.get("uiAmountString") or acc.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        holders.append({"wallet": owner, "token_account": address, "balance": balance})

    # Ranked, non-zero only.
    holders.sort(key=lambda h: h["balance"], reverse=True)
    holders = [h for h in holders if h["balance"] > 0]

    top10_share = None
    try:
        supply_raw = await sol.get_token_supply(mint)
        if supply_raw and supply_raw.get("uiAmountString"):
            supply = Decimal(str(supply_raw["uiAmountString"]))
            if supply > 0:
                top10_share = float(
                    sum(Decimal(str(h["balance"])) for h in holders[:10]) / supply
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("top_holders supply failed for {}: {}", mint, exc)

    payload = {
        "total_holders": len(holders),
        "top_holders": holders,
        "top10_share": top10_share,
        "as_of": token_intel._now_iso(),
        "source": "getTokenLargestAccounts",
    }

    # If RPC gave us nothing, fall back to the watchlist-derived snapshot.
    if not holders:
        fallback = await token_intel._load_holder_snapshot(db, mint)
        if fallback:
            payload = {**fallback, "source": "watchlist-fallback"}

    token_intel._cache_put("top_holders", mint, payload)
    token_intel._db_cache_put(db, "top_holders", mint, payload)
    return payload


async def early_buyers(db: Client, mint: str, *, limit: int = 10) -> list[dict]:
    """First-buy evidence for the current top holders of a mint.

    For each top holder, page its recent signatures and find the earliest
    incoming transfer of `mint`; record {wallet, first_buy_time, amount,
    signature}. Best-effort per holder — a holder with no parseable history is
    skipped, and the list is sorted by first-buy time ascending.
    """
    holders = await top_holders(db, mint)
    top = (holders.get("top_holders") or [])[:limit]
    sol = get_solana_service()

    buyers: list[dict] = []
    for h in top:
        wallet = h["wallet"]
        try:
            sigs = await sol.get_signatures_for_address(
                wallet, limit=EARLY_BUYER_SCAN_SIGNATURES
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Early-buyer sigs failed for {}: {}", wallet, exc)
            continue

        # Signatures come newest-first; walk backwards to the oldest buy.
        earliest: Optional[dict] = None
        for entry in reversed(sigs):
            if entry.get("err") is not None:
                continue
            signature = entry.get("signature")
            if not signature:
                continue
            try:
                parsed = await sol.get_parsed_transaction(signature)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Early-buyer tx parse failed for {}: {}", signature, exc)
                continue
            if not parsed:
                continue
            for t in token_intel._transfers_of_mint(parsed, mint, wallet):
                if t["direction"] != "in":
                    continue
                earliest = {
                    "wallet": wallet,
                    "amount": float(t["ui_amount"]),
                    "signature": signature,
                    "block_time": entry.get("blockTime"),
                }
                break  # oldest buy found for this holder
            if earliest is not None:
                break

        if earliest is not None:
            buyers.append(earliest)

    buyers.sort(key=lambda b: b.get("block_time") or 0)
    return buyers


# ---------------------------------------------------------------------------
# Wallet clustering
# ---------------------------------------------------------------------------

class _UnionFind:
    """Disjoint-set for merging wallet clusters."""

    def __init__(self, items: list[str]):
        self._parent = {x: x for x in items}
        self._rank = {x: 0 for x in items}

    def find(self, x: str) -> str:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1

    def groups(self) -> list[list[str]]:
        clusters: dict[str, list[str]] = defaultdict(list)
        for x in self._parent:
            clusters[self.find(x)].append(x)
        return [g for g in clusters.values() if len(g) > 1]


async def _trace_funding_source(sol, wallet: str) -> Optional[str]:
    """Find the wallet that first funded `wallet` with SOL.

    Pages the earliest signatures and looks for a System Program transfer
    or createAccount instruction where `wallet` is the destination.
    """
    try:
        sigs = await sol.get_signatures_for_address(wallet, limit=_FUNDING_SCAN_SIGNATURES)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Funding trace sigs failed for {}: {}", wallet, exc)
        return None

    # Signatures are newest-first; walk backwards to find the earliest funding tx.
    for entry in reversed(sigs):
        if entry.get("err") is not None:
            continue
        signature = entry.get("signature")
        if not signature:
            continue
        try:
            parsed = await sol.get_parsed_transaction(signature)
        except Exception:  # noqa: BLE001
            continue
        if not parsed:
            continue

        instrs = (parsed.get("transaction", {}).get("message", {}).get("instructions") or [])
        for ix in instrs:
            program = ix.get("program") or ""
            parsed_ix = ix.get("parsed") or {}
            # System Program transfer or createAccount
            if program == "system" and parsed_ix.get("type") in ("transfer", "createAccount"):
                info = parsed_ix.get("info", {})
                dest = info.get("destination") or info.get("newAccount")
                if dest == wallet:
                    source = info.get("source") or info.get("lamports")
                    if source and source != wallet:
                        return source
    return None


async def _get_wallet_holdings(sol, wallet: str) -> set[str]:
    """Return the set of mint addresses held by a wallet."""
    try:
        accounts = await sol.get_token_accounts_by_owner(wallet)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Holdings fetch failed for {}: {}", wallet, exc)
        return set()

    mints: set[str] = set()
    for acc in accounts[:_HOLDING_SCAN_LIMIT]:
        parsed = acc.get("account", {}).get("data", {}).get("parsed", {})
        info = parsed.get("info", {})
        mint = info.get("mint")
        if mint:
            mints.add(mint)
    return mints


def _jaccard(a: set, b: set) -> float:
    """Jaccard similarity between two sets."""
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union > 0 else 0.0


async def detect_clusters(
    db: Client, mint: str, *, limit: int = 20
) -> dict:
    """Detect coordinated wallet clusters among top holders.

    Three signals are evaluated:
    1. Shared funding — wallets funded by the same source wallet.
    2. Coordinated timing — first buys within ±60 seconds of each other.
    3. Overlapping holdings — Jaccard similarity > 0.5 on held mints.

    Signals are merged via union-find. Returns cluster list with confidence
    scores. Fail-soft: a wallet that errors during analysis is skipped.
    """
    holders = await top_holders(db, mint)
    top = (holders.get("top_holders") or [])[:limit]
    if len(top) < 2:
        return {
            "mint": mint,
            "clusters": [],
            "total_wallets_analyzed": len(top),
            "as_of": token_intel._now_iso(),
        }

    sol = get_solana_service()
    wallets = [h["wallet"] for h in top]

    # --- Gather per-wallet data ---
    funding_sources: dict[str, Optional[str]] = {}
    first_buy_times: dict[str, Optional[int]] = {}
    holdings: dict[str, set[str]] = {}

    for wallet in wallets:
        funding_sources[wallet] = await _trace_funding_source(sol, wallet)
        holdings[wallet] = await _get_wallet_holdings(sol, wallet)

    # First buy times from early_buyers data.
    buyers = await early_buyers(db, mint, limit=limit)
    for b in buyers:
        first_buy_times[b["wallet"]] = b.get("block_time")

    # --- Build signal pairs ---
    # Each signal maps (wallet_a, wallet_b) -> signal_name
    signal_pairs: dict[tuple[str, str], set[str]] = defaultdict(set)

    for i, w_a in enumerate(wallets):
        for w_b in wallets[i + 1:]:
            pair = (w_a, w_b)

            # Signal 1: shared funding
            fa, fb = funding_sources.get(w_a), funding_sources.get(w_b)
            if fa and fb and fa == fb:
                signal_pairs[pair].add("shared_funding")

            # Signal 2: coordinated timing
            ta, tb = first_buy_times.get(w_a), first_buy_times.get(w_b)
            if ta and tb and abs(ta - tb) <= _COORDINATED_TIMING_WINDOW_SECONDS:
                signal_pairs[pair].add("coordinated_timing")

            # Signal 3: overlapping holdings
            ha, hb = holdings.get(w_a, set()), holdings.get(w_b, set())
            if ha and hb and _jaccard(ha, hb) >= _HOLDINGS_JACCARD_THRESHOLD:
                signal_pairs[pair].add("overlapping_holdings")

    # --- Merge via union-find ---
    uf = _UnionFind(wallets)
    for (w_a, w_b), signals in signal_pairs.items():
        if signals:
            uf.union(w_a, w_b)

    # --- Build cluster output ---
    groups = uf.groups()
    clusters = []
    for group in groups:
        group_set = set(group)
        cluster_signals: set[str] = set()
        for (w_a, w_b), signals in signal_pairs.items():
            if w_a in group_set and w_b in group_set:
                cluster_signals.update(signals)

        # Confidence: 1 signal = 0.33, 2 = 0.67, 3 = 1.0
        confidence = min(len(cluster_signals) / 3.0, 1.0)

        clusters.append({
            "id": str(uuid.uuid4())[:8],
            "wallets": sorted(group),
            "signals": sorted(cluster_signals),
            "confidence": round(confidence, 2),
        })

    # Sort by confidence descending.
    clusters.sort(key=lambda c: c["confidence"], reverse=True)

    return {
        "mint": mint,
        "clusters": clusters,
        "total_wallets_analyzed": len(wallets),
        "as_of": token_intel._now_iso(),
    }
