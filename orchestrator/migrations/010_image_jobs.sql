-- ============================================================================
-- Orvix Orchestrator — migration 010: image generation jobs
-- Run AFTER 001-009. Idempotent. Applied via scripts/migrate.py.
-- ============================================================================

begin;

create table if not exists image_jobs (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid references users(id) on delete set null,
    provider_id  uuid references users(id) on delete set null,
    model        text not null,
    prompt       text,                       -- truncated to 500 chars by the app
    width        integer not null,
    height       integer not null,
    cost_usdc    numeric(20,6) default 0,    -- 0 during alpha; billing in a later phase
    image_url    text not null,
    created_at   timestamptz not null default now(),
    expires_at   timestamptz not null        -- created_at + 24h; used by the cleanup job
);

create index if not exists idx_image_jobs_user_id    on image_jobs (user_id);
create index if not exists idx_image_jobs_expires_at on image_jobs (expires_at);

-- RLS: service_role only, matching the rest of the schema.
alter table image_jobs enable row level security;
drop policy if exists service_role_all on image_jobs;
create policy service_role_all on image_jobs for all to service_role using (true) with check (true);

commit;
