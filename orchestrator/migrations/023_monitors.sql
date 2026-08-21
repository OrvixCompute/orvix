-- ============================================================================
-- Orvix Orchestrator — migration 023: monitoring agents (asset monitors)
-- Run AFTER 001-022. Idempotent. Applied via scripts/migrate.py.
--
-- Users deploy "monitors" that watch a token or wallet and emit alert events
-- when configured conditions fire. Evaluation runs in the background
-- MonitorService worker (ENABLE_MONITOR_WORKER). last_cursor is the worker's
-- per-monitor bookmark into on-chain signature history for activity conditions.
-- ============================================================================

begin;

create table if not exists monitors (
    id                  uuid primary key default gen_random_uuid(),
    user_id             uuid not null references users(id) on delete cascade,
    name                text not null default '',
    target_type         text not null check (target_type in ('token', 'wallet')),
    target_address      text not null,
    conditions          jsonb not null,          -- array of condition objects
    webhook_url         text,
    is_active           boolean not null default true,
    interval_minutes    integer not null default 30,
    baseline_price_usdc numeric(30,10),          -- set at creation for price-drop conditions
    last_checked_at     timestamptz,
    last_cursor         text,                    -- signature cursor for new-activity detection
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

create index if not exists idx_monitors_active on monitors (is_active) where is_active;
create index if not exists idx_monitors_user on monitors (user_id);

-- RLS: service_role only, matching the rest of the schema.
alter table monitors enable row level security;
drop policy if exists service_role_all on monitors;
create policy service_role_all on monitors for all to service_role using (true) with check (true);

commit;
