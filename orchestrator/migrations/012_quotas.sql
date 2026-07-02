-- ============================================================================
-- Orvix Orchestrator — migration 012: quota + holder-status tracking
-- Run AFTER 001-011. Idempotent. Applied via scripts/migrate.py.
-- ============================================================================

begin;

-- Cached ORVX holder status per wallet (refreshed every HOLDER_CACHE_TTL_MINUTES).
create table if not exists holder_status (
    wallet_address   text primary key,
    orvx_balance     numeric(30,6) not null default 0,
    is_holder        boolean not null default false,
    last_checked_at  timestamptz not null default now()
);

-- Non-holder lifetime free chat allowance.
create table if not exists chat_quota_usage (
    wallet_address     text primary key,
    lifetime_free_used integer not null default 0,
    first_used_at      timestamptz,
    last_used_at       timestamptz
);

-- Per-day image generation counter (resets at 00:00 UTC by date rollover).
create table if not exists image_quota_usage (
    wallet_address  text not null,
    usage_date      date not null,   -- UTC date
    count           integer not null default 0,
    primary key (wallet_address, usage_date)
);

create index if not exists idx_image_quota_wallet        on image_quota_usage (wallet_address);
create index if not exists idx_holder_status_last_checked on holder_status (last_checked_at);

-- RLS: service_role only, matching the rest of the schema.
alter table holder_status     enable row level security;
alter table chat_quota_usage  enable row level security;
alter table image_quota_usage enable row level security;
drop policy if exists service_role_all on holder_status;
drop policy if exists service_role_all on chat_quota_usage;
drop policy if exists service_role_all on image_quota_usage;
create policy service_role_all on holder_status     for all to service_role using (true) with check (true);
create policy service_role_all on chat_quota_usage  for all to service_role using (true) with check (true);
create policy service_role_all on image_quota_usage for all to service_role using (true) with check (true);

commit;
