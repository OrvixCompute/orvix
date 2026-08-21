-- ============================================================================
-- Orvix Orchestrator — migration 022: token intelligence scan cache
-- Run AFTER 001-021. Idempotent. Applied via scripts/migrate.py.
--
-- Cached results of token / wallet / accumulation scans. Reused by the public
-- endpoints and by the monitor worker, so repeated evaluation does not hammer
-- Solana JSON-RPC. The holder/whale analysis is anchored on explicit watchlists
-- (TOKEN_WHALE_WATCHLIST_JSON / TOKEN_POOLS_JSON) — plain Solana JSON-RPC
-- cannot enumerate a mint's holders, and holder snapshots live inside payload
-- rows written by scans/watchlist refresh.
-- ============================================================================

begin;

create table if not exists intel_scans (
    scan_type   text not null check (scan_type in ('token', 'wallet', 'accumulation')),
    target      text not null,          -- mint CA / wallet address
    payload     jsonb not null,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    primary key (scan_type, target)
);

create index if not exists idx_intel_scans_updated on intel_scans (updated_at desc);

-- RLS: service_role only, matching the rest of the schema.
alter table intel_scans enable row level security;
drop policy if exists service_role_all on intel_scans;
create policy service_role_all on intel_scans for all to service_role using (true) with check (true);

commit;
