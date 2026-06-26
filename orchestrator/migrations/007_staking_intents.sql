-- ============================================================================
-- Orvix Orchestrator — migration 007: staking intents
-- Run AFTER 006. Idempotent. A staking intent is a pending ORVX deposit the user
-- fulfills on-chain with a unique memo ("orvix_stake_<random>"). The payment
-- listener matches the memo and credits users.staked_orvx via the stake_orvx RPC.
-- ============================================================================

begin;

create table if not exists staking_intents (
    id               uuid primary key default gen_random_uuid(),
    user_id          uuid references users(id) on delete cascade,
    memo             text unique not null,            -- "orvix_stake_<random12>"
    expected_amount  numeric(20,9) not null,
    status           text not null default 'pending'
                         check (status in ('pending','fulfilled','expired')),
    fulfilled_at     timestamptz,
    expires_at       timestamptz not null,
    created_at       timestamptz not null default now()
);

comment on table staking_intents is 'Pending ORVX stake deposits awaiting an on-chain transfer matched by memo.';

create index if not exists idx_staking_intents_user_id        on staking_intents (user_id);
create index if not exists idx_staking_intents_memo           on staking_intents (memo);
create index if not exists idx_staking_intents_status_expires on staking_intents (status, expires_at);

-- RLS: service_role only, matching the rest of the schema.
alter table staking_intents enable row level security;
drop policy if exists service_role_all on staking_intents;
create policy service_role_all on staking_intents for all to service_role using (true) with check (true);

commit;
