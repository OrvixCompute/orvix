# Multi-Worker Readiness Audit

> Last updated: 2026-08-23

This document catalogs all in-memory state in the orchestrator that affects multi-worker deployments.

## Summary

| Component | File | State Type | Risk | Status |
|---|---|---|---|---|
| **Rate limiter** | `rate_limit_service.py` | Redis sorted-set (or in-memory fallback) | Critical | **Fixed** — Redis backend added |
| **Challenge store** | `auth_service.py` | Redis string with TTL (or DB fallback) | Critical | **Fixed** — Redis backend added |
| **Node registry** | `node_manager.py` | `dict[str, NodeConnection]` with WebSockets + Futures | Critical | **Unresolved** — WebSocket connections are inherently process-bound |
| **Intel scan cache** | `token_intel.py` | `dict[tuple, tuple[float, dict]]` | High | Unresolved |
| **Network stats cache** | `network_stats_service.py` | Module-level `_cache` tuple | High | Unresolved |
| **Payment listener** | `payment_listener.py` | `asyncio.Task` + cursor dict + token account sets | High | Unresolved |
| **Payout worker** | `payout_service.py` | `asyncio.Task` + `asyncio.Event` | Medium | Unresolved |
| **Monitor worker** | `monitor_service.py` | `asyncio.Task` + `asyncio.Event` | Medium | Unresolved |
| **Supabase client** | `database.py` | Global `_client` | Low | OK — per-process is fine |
| **Solana RPC client** | `solana_service.py` | Global `_service` with `httpx.AsyncClient` | Low | OK — per-process is fine |
| **Covenant client** | `covenant_service.py` | Global `_service` with `_initialized` flag | Low | OK — redundant handshake only |
| **Settings** | `config.py` | `lru_cache` singleton | Low | OK — immutable after startup |

## Critical: Node Registry

**File:** `orchestrator/app/services/node_manager.py`

```python
node_manager = NodeManager()

class NodeManager:
    def __init__(self) -> None:
        self.connected_nodes: dict[str, NodeConnection] = {}
```

Each `NodeConnection` holds:
- `websocket` — the live WebSocket to the GPU node
- `pending_jobs: dict[str, PendingJob]` — in-flight `asyncio.Future` / `asyncio.Queue` objects
- `current_jobs: int` — live concurrency counter
- `last_heartbeat` — heartbeat timestamp

**Why it breaks multi-worker:** Only the worker that accepted the WebSocket can see the node. Other workers cannot route jobs to it, see its capacity, or correlate responses.

**Mitigation options:**
1. **Sticky sessions** — route all requests for a given node to the same worker (requires load balancer support)
2. **Redis pub/sub** — broadcast node state changes to all workers (complex, adds latency)
3. **Dedicated routing service** — separate process that owns all WebSocket connections (architectural change)

## Critical: Rate Limiter (RESOLVED)

**File:** `orchestrator/app/services/rate_limit_service.py`

Now supports Redis backend via `REDIS_URL` config. When set, uses sorted-set sliding window shared across all workers. Falls back to in-memory when Redis is unavailable.

## Critical: Challenge Store (RESOLVED)

**File:** `orchestrator/app/services/auth_service.py`

Now supports Redis backend via `REDIS_URL` config. When set, uses Redis strings with native TTL for automatic expiry. Falls back to DB when Redis is unavailable.

## High: Intel Scan Cache

**File:** `orchestrator/app/services/token_intel.py`

```python
_scan_cache: dict[tuple[str, str], tuple[float, dict]] = {}
```

Each worker maintains its own cache. One worker's cache miss triggers expensive Solana RPC + Jupiter API calls even if another worker already has the result.

**Mitigation:** The DB-backed `intel_scans` table mitigates this (second-tier cache), but the in-memory layer is always checked first and is never shared.

## High: Network Stats Cache

**File:** `orchestrator/app/services/network_stats_service.py`

```python
_cache: tuple[float, dict] | None = None
```

Caches the aggregated network stats response for `NETWORK_STATS_CACHE_SECONDS` (default 30s). Also reads live node counts from `node_manager.connected_nodes`.

**Mitigation:** Different workers report different node counts. The public `/v1/network/stats` endpoint would return inconsistent data depending on which worker handles the request.

## High: Payment Listener

**File:** `orchestrator/app/services/payment_listener.py`

```python
class PaymentListener:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_signature: dict[str, str] = {}
        self._treasury_token_accounts: set[str] = set()
        self._treasury_orvx_token_accounts: set[str] = set()
```

If multiple workers start the payment listener (when `ENABLE_PAYMENT_LISTENER=true`), they'll race to process the same signatures. The DB-level idempotency (`credit_topup` RPC) prevents double-crediting, but the workers waste RPC calls.

**Mitigation:** Only one worker should run this. Use a distributed lock (e.g., Redis advisory lock) or run in a dedicated worker process.

## Medium: Background Workers

**Files:** `orchestrator/app/services/payout_service.py`, `orchestrator/app/services/monitor_service.py`

Each holds an `asyncio.Task` and `asyncio.Event` for lifecycle management. If multiple workers enable these, they'll race on the same DB rows.

**Mitigation:** DB-level guards (status transitions to `processing`) provide some protection, but duplicate work and timing issues remain. Use distributed locks or dedicated worker processes.

## Recommendations

1. **Set `REDIS_URL`** in production to enable Redis-backed rate limiting and challenge storage
2. **Run background workers in a dedicated process** — payment listener, payout worker, and monitor worker should not run in every worker
3. **Accept eventual consistency** for caches (intel scans, network stats) — the DB is the source of truth
4. **Document single-process assumptions** for the node registry — this is the hardest problem and may require architectural changes

## Config Flag

Add to `config.py`:

```python
WORKERS: int = Field(1, description="Number of uvicorn workers. When > 1, REDIS_URL is required.")
```

With validation:

```python
@field_validator("WORKERS")
@classmethod
def _validate_workers(cls, v: int) -> int:
    if v > 1 and not settings.REDIS_URL:
        raise ValueError("REDIS_URL is required when WORKERS > 1")
    return v
```
