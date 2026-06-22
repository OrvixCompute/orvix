-- ============================================================================
-- Orvix Orchestrator — initial schema (Prompt 2)
-- Paste this whole file into the Supabase SQL Editor and run it.
-- Safe to re-run: uses IF NOT EXISTS / CREATE OR REPLACE where possible.
-- ============================================================================

-- gen_random_uuid() lives in pgcrypto (usually pre-installed on Supabase).
create extension if not exists pgcrypto;

-- ----------------------------------------------------------------------------
-- users — one row per wallet. Holds tier and the USDC balance.
-- ----------------------------------------------------------------------------
create table if not exists users (
    id              uuid primary key default gen_random_uuid(),
    wallet_address  text unique not null,                 -- Solana base58
    email           text,
    tier            text not null default 'bronze'
                        check (tier in ('bronze','silver','gold','diamond')),
    balance_usdc    numeric(20,6) not null default 0 check (balance_usdc >= 0),
    is_active       boolean not null default true,
    last_active_at  timestamptz not null default now(),
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create index if not exists idx_users_wallet         on users (wallet_address);
create index if not exists idx_users_tier           on users (tier);
create index if not exists idx_users_last_active_at on users (last_active_at);

-- ----------------------------------------------------------------------------
-- api_keys — sha256 hashes of developer API keys. Plaintext is never stored.
-- ----------------------------------------------------------------------------
create table if not exists api_keys (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references users(id) on delete cascade,
    key_hash      text unique not null,                   -- sha256 hex
    key_prefix    text not null,                          -- first 12 chars, for display
    name          text not null,
    is_active     boolean not null default true,
    last_used_at  timestamptz,
    created_at    timestamptz not null default now()
);

create index if not exists idx_api_keys_user_id   on api_keys (user_id);
create index if not exists idx_api_keys_key_hash  on api_keys (key_hash);
create index if not exists idx_api_keys_is_active on api_keys (is_active);

-- ----------------------------------------------------------------------------
-- topup_intents — a pending deposit the user will fulfill on-chain with a memo.
-- ----------------------------------------------------------------------------
create table if not exists topup_intents (
    id                    uuid primary key default gen_random_uuid(),
    user_id               uuid not null references users(id) on delete cascade,
    memo                  text unique not null,           -- "orvx_<random12>"
    expected_amount_usdc  numeric(20,6),
    status                text not null default 'pending'
                              check (status in ('pending','fulfilled','expired','partial')),
    fulfilled_at          timestamptz,
    expires_at            timestamptz not null,
    created_at            timestamptz not null default now()
);

create index if not exists idx_topup_user_id          on topup_intents (user_id);
create index if not exists idx_topup_memo             on topup_intents (memo);
create index if not exists idx_topup_status_expires   on topup_intents (status, expires_at);

-- ----------------------------------------------------------------------------
-- transactions — ledger of all balance-affecting events (idempotent on signature).
-- ----------------------------------------------------------------------------
create table if not exists transactions (
    id                 uuid primary key default gen_random_uuid(),
    user_id            uuid not null references users(id) on delete cascade,
    type               text not null
                           check (type in ('topup','withdraw','inference_charge','provider_payout','refund')),
    amount             numeric(20,6) not null,
    token              text not null default 'USDC'
                           check (token in ('USDC')),
    solana_signature   text unique,                       -- null for off-chain charges
    status             text not null default 'pending'
                           check (status in ('pending','confirmed','failed')),
    metadata           jsonb not null default '{}',
    created_at         timestamptz not null default now()
);

create index if not exists idx_tx_user_created on transactions (user_id, created_at desc);
create index if not exists idx_tx_signature    on transactions (solana_signature);
create index if not exists idx_tx_status       on transactions (status);

-- ----------------------------------------------------------------------------
-- jobs — one row per inference request (mock for now). Drives usage/billing.
-- ----------------------------------------------------------------------------
create table if not exists jobs (
    id                     uuid primary key default gen_random_uuid(),
    user_id                uuid not null references users(id) on delete cascade,
    api_key_id             uuid references api_keys(id) on delete set null,
    node_id                uuid,                           -- nodes table comes later
    model                  text not null,
    prompt_tokens          integer not null default 0,
    completion_tokens      integer not null default 0,
    total_tokens           integer generated always as (prompt_tokens + completion_tokens) stored,
    cost_usdc              numeric(20,6) not null default 0,
    provider_earning_usdc  numeric(20,6) not null default 0,
    latency_ms             integer,
    status                 text not null default 'completed'
                               check (status in ('completed','failed','timeout','refunded')),
    error_message          text,
    is_mock                boolean not null default false,
    created_at             timestamptz not null default now()
);

create index if not exists idx_jobs_user_created on jobs (user_id, created_at desc);
create index if not exists idx_jobs_api_key_id   on jobs (api_key_id);
create index if not exists idx_jobs_status       on jobs (status);
create index if not exists idx_jobs_model        on jobs (model);

-- ============================================================================
-- TRIGGERS
-- ============================================================================

-- Keep users.updated_at fresh on every UPDATE.
create or replace function set_updated_at() returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists trg_users_updated_at on users;
create trigger trg_users_updated_at
    before update on users
    for each row execute function set_updated_at();

-- Bump users.last_active_at whenever a job is inserted for that user.
create or replace function bump_last_active() returns trigger as $$
begin
    update users set last_active_at = now() where id = new.user_id;
    return new;
end;
$$ language plpgsql;

drop trigger if exists trg_jobs_bump_active on jobs;
create trigger trg_jobs_bump_active
    after insert on jobs
    for each row execute function bump_last_active();

-- ============================================================================
-- ATOMIC BALANCE FUNCTIONS
-- Called by the backend via supabase.rpc(...). The check-and-update happens in
-- a single statement, so concurrent inference requests can't race the balance.
-- ============================================================================

-- Deduct USDC. Returns the new balance, or NULL if funds are insufficient.
create or replace function deduct_balance(p_user_id uuid, p_amount numeric)
returns numeric as $$
declare
    new_balance numeric;
begin
    update users
        set balance_usdc = balance_usdc - p_amount
        where id = p_user_id and balance_usdc >= p_amount
        returning balance_usdc into new_balance;
    return new_balance;  -- NULL when the WHERE matched no row (insufficient funds)
end;
$$ language plpgsql;

-- Credit USDC. Returns the new balance.
create or replace function credit_balance(p_user_id uuid, p_amount numeric)
returns numeric as $$
declare
    new_balance numeric;
begin
    update users
        set balance_usdc = balance_usdc + p_amount
        where id = p_user_id
        returning balance_usdc into new_balance;
    return new_balance;
end;
$$ language plpgsql;

-- ============================================================================
-- ROW LEVEL SECURITY
-- The backend connects with the service_role key, which bypasses RLS, but we
-- enable RLS + an explicit service_role policy and grant NO public access.
-- ============================================================================
alter table users         enable row level security;
alter table api_keys      enable row level security;
alter table topup_intents enable row level security;
alter table transactions  enable row level security;
alter table jobs          enable row level security;

do $$
declare t text;
begin
    foreach t in array array['users','api_keys','topup_intents','transactions','jobs']
    loop
        execute format('drop policy if exists service_role_all on %I', t);
        execute format(
            'create policy service_role_all on %I for all to service_role using (true) with check (true)',
            t
        );
    end loop;
end $$;

-- ============================================================================
-- SEED DATA (for local testing)
-- ============================================================================

-- Test user with a known wallet. (Valid base58; this keypair is for dev only.)
insert into users (wallet_address, email, tier, balance_usdc)
values ('5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9', 'test@orvix.local', 'gold', 1000)
on conflict (wallet_address) do nothing;

-- Test API key for that user.
--   PLAINTEXT KEY (use this in Authorization: Bearer ...):
--       orvx_sk_testkey0testkey0testkey0testkey0
--   sha256 = a0392d76ac186acd5f934a464936bf769dda3177b67fd308221b3cd65f7be124
insert into api_keys (user_id, key_hash, key_prefix, name)
select id,
       'a0392d76ac186acd5f934a464936bf769dda3177b67fd308221b3cd65f7be124',
       'orvx_sk_test',
       'Local test key'
from users
where wallet_address = '5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9'
on conflict (key_hash) do nothing;
