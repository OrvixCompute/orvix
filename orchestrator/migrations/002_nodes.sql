-- ============================================================================
-- Orvix Orchestrator — migration 002: GPU nodes (Prompt 5)
-- Run AFTER 001_initial_schema.sql. Idempotent.
-- ============================================================================

create table if not exists nodes (
    id                  uuid primary key default gen_random_uuid(),
    provider_id         uuid references users(id) on delete cascade,
    name                text,
    status              text default 'offline'
                            check (status in ('offline','ready','busy','draining')),
    gpu_model           text,
    vram_mb             integer,
    models_supported    text[],
    max_concurrent_jobs integer default 1,
    total_jobs          integer default 0,
    total_earned_usdc   numeric(20,6) default 0,
    reputation_score    integer default 100,
    last_heartbeat      timestamptz,
    created_at          timestamptz default now(),
    updated_at          timestamptz default now()
);

create index if not exists idx_nodes_provider_id   on nodes (provider_id);
create index if not exists idx_nodes_status_model  on nodes (status, models_supported);

-- Keep nodes.updated_at fresh (reuses set_updated_at() from migration 001).
drop trigger if exists trg_nodes_updated_at on nodes;
create trigger trg_nodes_updated_at
    before update on nodes
    for each row execute function set_updated_at();

-- jobs.node_id and jobs.is_mock already exist from migration 001; ensure they do.
alter table jobs add column if not exists node_id uuid references nodes(id) on delete set null;
alter table jobs add column if not exists is_mock boolean default true;

-- RLS to match the rest of the schema (service_role full access; no public).
alter table nodes enable row level security;
drop policy if exists service_role_all on nodes;
create policy service_role_all on nodes for all to service_role using (true) with check (true);
