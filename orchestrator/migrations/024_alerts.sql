-- ============================================================================
-- Orvix Orchestrator — migration 024: alert events + webhook delivery outbox
-- Run AFTER 001-023. Idempotent. Applied via scripts/migrate.py.
--
-- alert_events: one row per fired condition. The unique (monitor_id, dedup_key)
-- index is the backstop against duplicate alerts — the worker also selects
-- before inserting, and the constraint catches races.
-- alert_webhooks: delivery outbox. Rows are created with the alert; the worker
-- POSTs the payload to webhook_url with exponential backoff
-- (next_retry_at = now + 2^(attempts-1) * WEBHOOK_RETRY_BASE_SECONDS).
-- ============================================================================

begin;

create table if not exists alert_events (
    id             uuid primary key default gen_random_uuid(),
    monitor_id     uuid not null references monitors(id) on delete cascade,
    user_id        uuid not null references users(id) on delete cascade,
    condition_type text not null,
    message        text not null,
    payload        jsonb not null,
    dedup_key      text not null,        -- monitor+condition+signature | condition+utc-date
    occurred_at    timestamptz not null default now(),
    created_at     timestamptz not null default now()
);

create unique index if not exists idx_alert_dedup on alert_events (monitor_id, dedup_key);
create index if not exists idx_alert_events_monitor on alert_events (monitor_id, occurred_at desc);

create table if not exists alert_webhooks (
    id             uuid primary key default gen_random_uuid(),
    alert_event_id uuid not null references alert_events(id) on delete cascade,
    monitor_id     uuid not null references monitors(id) on delete cascade,
    webhook_url    text not null,
    payload        jsonb not null,
    status         text not null default 'pending',  -- pending|sending|delivered|failed
    attempts       integer not null default 0,
    next_retry_at  timestamptz,
    last_error     text,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

create index if not exists idx_alert_webhooks_pending
    on alert_webhooks (status, next_retry_at) where status in ('pending', 'sending');

-- RLS: service_role only, matching the rest of the schema.
alter table alert_events enable row level security;
alter table alert_webhooks enable row level security;
drop policy if exists service_role_all on alert_events;
drop policy if exists service_role_all on alert_webhooks;
create policy service_role_all on alert_events for all to service_role using (true) with check (true);
create policy service_role_all on alert_webhooks for all to service_role using (true) with check (true);

commit;
