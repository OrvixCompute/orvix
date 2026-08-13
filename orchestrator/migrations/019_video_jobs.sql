-- ============================================================================
-- Orvix Orchestrator — migration 019: video generation jobs + quota
-- Run AFTER 001-018. Idempotent. Applied via scripts/migrate.py.
-- ============================================================================

begin;

create table if not exists video_jobs (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid references users(id) on delete set null,
    provider_id  uuid references users(id) on delete set null,
    model        text not null,
    prompt       text,                       -- truncated to 500 chars by the app
    width        integer not null,
    height       integer not null,
    num_frames   integer not null,
    fps          integer not null,
    cost_usdc    numeric(20,6) default 0,    -- 0 during alpha; billing in a later phase
    video_url    text not null,
    created_at   timestamptz not null default now(),
    expires_at   timestamptz not null        -- created_at + 24h; used by the cleanup job
);

create index if not exists idx_video_jobs_user_id    on video_jobs (user_id);
create index if not exists idx_video_jobs_expires_at on video_jobs (expires_at);

-- Per-day video generation counter (resets at 00:00 UTC by date rollover).
create table if not exists video_quota_usage (
    wallet_address  text not null,
    usage_date      date not null,   -- UTC date
    count           integer not null default 0,
    primary key (wallet_address, usage_date)
);

create index if not exists idx_video_quota_wallet on video_quota_usage (wallet_address);

-- RLS: service_role only, matching the rest of the schema.
alter table video_jobs         enable row level security;
alter table video_quota_usage  enable row level security;
drop policy if exists service_role_all on video_jobs;
drop policy if exists service_role_all on video_quota_usage;
create policy service_role_all on video_jobs        for all to service_role using (true) with check (true);
create policy service_role_all on video_quota_usage for all to service_role using (true) with check (true);

commit;
