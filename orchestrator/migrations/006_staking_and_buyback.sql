-- ============================================================================
-- Orvix Orchestrator — migration 006: provider staking & buyback/burn accounting
-- Run AFTER 001-005. Idempotent (if not exists / on conflict / create or replace).
-- Implements the whitepaper's ORVX utility model:
--   * providers stake ORVX (custodial, off-chain ledger backed by on-chain deposits)
--   * tier is stake-based (see migration 008)
--   * 50/30/20 split of the platform fee into buyback / treasury / operations
--   * bought-back ORVX is held, then burned monthly
-- Wrapped in a single transaction so a failure leaves the schema untouched.
-- ============================================================================

begin;

-- ----------------------------------------------------------------------------
-- 1. Staking columns on users.
--    staked_orvx is the custodial balance the treasury holds on the user's
--    behalf; it gates provider eligibility and drives the tier system.
-- ----------------------------------------------------------------------------
alter table users add column if not exists staked_orvx numeric(20,9) not null default 0
    check (staked_orvx >= 0);
alter table users add column if not exists stake_locked_until timestamptz;  -- reserved for lock-period logic

comment on column users.staked_orvx is
    'ORVX staked in custody (off-chain ledger, held by treasury). Drives provider eligibility and tier.';
comment on column users.stake_locked_until is
    'If set, staked ORVX cannot be unstaked before this time. Reserved for future lock-period rules.';

-- ----------------------------------------------------------------------------
-- 2. stakes — append-only log of stake / unstake / slash events.
-- ----------------------------------------------------------------------------
create table if not exists stakes (
    id                uuid primary key default gen_random_uuid(),
    user_id           uuid references users(id) on delete cascade,
    type              text not null check (type in ('stake','unstake','slash')),
    amount            numeric(20,9) not null check (amount > 0),
    solana_signature  text unique,            -- on-chain deposit / withdrawal tx (null for internal adjustments)
    reason            text,                    -- e.g. "provider registration", "tier upgrade", "slashed"
    metadata          jsonb not null default '{}',
    created_at        timestamptz not null default now()
);

comment on table stakes is 'Append-only audit log of every stake, unstake, and slash event.';

create index if not exists idx_stakes_user_id_created on stakes (user_id, created_at desc);
create index if not exists idx_stakes_type            on stakes (type);

-- ----------------------------------------------------------------------------
-- 3. Per-job revenue split columns (the platform fee broken into its 3 buckets).
-- ----------------------------------------------------------------------------
alter table jobs add column if not exists buyback_budget_usdc numeric(20,6) not null default 0;
alter table jobs add column if not exists treasury_usdc       numeric(20,6) not null default 0;
alter table jobs add column if not exists operations_usdc     numeric(20,6) not null default 0;

comment on column jobs.buyback_budget_usdc is 'Portion of this job''s platform fee earmarked for ORVX buyback (50%).';
comment on column jobs.treasury_usdc       is 'Portion of this job''s platform fee sent to treasury reserves (30%).';
comment on column jobs.operations_usdc     is 'Portion of this job''s platform fee allocated to operations (20%).';

-- ----------------------------------------------------------------------------
-- 4. buyback_events — every USDC -> ORVX swap executed by an admin.
-- ----------------------------------------------------------------------------
create table if not exists buyback_events (
    id                              uuid primary key default gen_random_uuid(),
    usdc_spent                      numeric(20,6) not null check (usdc_spent > 0),
    orvx_received                   numeric(20,9) not null check (orvx_received > 0),
    execution_price_usdc_per_orvx   numeric(20,9) not null,
    solana_signature                text unique not null,   -- on-chain swap tx
    executed_by                     text not null,          -- admin wallet address
    notes                           text,
    created_at                      timestamptz not null default now()
);

comment on table buyback_events is 'Each open-market USDC -> ORVX buyback, verifiable on a Solana explorer.';

create index if not exists idx_buyback_events_created on buyback_events (created_at desc);

-- ----------------------------------------------------------------------------
-- 5. burn_events — every monthly burn of bought-back ORVX to the incinerator.
-- ----------------------------------------------------------------------------
create table if not exists burn_events (
    id                uuid primary key default gen_random_uuid(),
    orvx_burned       numeric(20,9) not null check (orvx_burned > 0),
    solana_signature  text unique not null,
    period_start      timestamptz not null,   -- start of the period this burn covers
    period_end        timestamptz not null,
    executed_by       text not null,
    notes             text,
    created_at        timestamptz not null default now(),
    check (period_end > period_start)
);

comment on table burn_events is 'Each ORVX burn (transfer to the incinerator), verifiable on a Solana explorer.';

create index if not exists idx_burn_events_created on burn_events (created_at desc);

-- ----------------------------------------------------------------------------
-- 6. global_accounting — singleton row holding treasury & buyback/burn totals.
-- ----------------------------------------------------------------------------
create table if not exists global_accounting (
    id                            integer primary key default 1 check (id = 1),
    buyback_budget_usdc           numeric(20,6) not null default 0 check (buyback_budget_usdc >= 0),
    treasury_balance_usdc         numeric(20,6) not null default 0 check (treasury_balance_usdc >= 0),
    operations_balance_usdc       numeric(20,6) not null default 0 check (operations_balance_usdc >= 0),
    orvx_held_for_burn            numeric(20,9) not null default 0 check (orvx_held_for_burn >= 0),
    total_orvx_burned             numeric(20,9) not null default 0,
    total_orvx_bought             numeric(20,9) not null default 0,
    total_usdc_spent_on_buyback   numeric(20,6) not null default 0,
    updated_at                    timestamptz not null default now()
);

comment on table global_accounting is 'Singleton (id=1) of platform-wide treasury, buyback budget, and burn totals.';

insert into global_accounting (id) values (1) on conflict (id) do nothing;

-- ============================================================================
-- ATOMIC FUNCTIONS
-- Called by the backend / admin tooling via supabase.rpc(...).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 7a. stake_orvx — credit a stake deposit. Idempotent on the on-chain signature.
--     Returns false if the signature was already recorded.
-- ----------------------------------------------------------------------------
create or replace function stake_orvx(
    p_user_id uuid, p_amount numeric, p_solana_sig text, p_reason text
)
returns boolean as $$
begin
    if p_solana_sig is not null
       and exists (select 1 from stakes where solana_signature = p_solana_sig) then
        return false;  -- already processed
    end if;

    update users set staked_orvx = staked_orvx + p_amount, updated_at = now()
        where id = p_user_id;

    insert into stakes (user_id, type, amount, solana_signature, reason)
        values (p_user_id, 'stake', p_amount, p_solana_sig, p_reason);

    return true;
end;
$$ language plpgsql;

-- ----------------------------------------------------------------------------
-- 7b. unstake_orvx — debit a stake. Locks the row, enforces the 25k provider
--     floor, and refuses to go negative. Returns false when not allowed.
-- ----------------------------------------------------------------------------
create or replace function unstake_orvx(
    p_user_id uuid, p_amount numeric, p_solana_sig text, p_reason text
)
returns boolean as $$
declare
    current_staked numeric;
    is_prov        boolean;
begin
    select staked_orvx, is_provider into current_staked, is_prov
        from users where id = p_user_id for update;

    if current_staked is null or current_staked < p_amount then
        return false;  -- insufficient stake
    end if;

    -- Providers must keep at least the 25,000 ORVX minimum staked.
    if is_prov and (current_staked - p_amount) < 25000 then
        return false;
    end if;

    update users set staked_orvx = staked_orvx - p_amount, updated_at = now()
        where id = p_user_id;

    insert into stakes (user_id, type, amount, solana_signature, reason)
        values (p_user_id, 'unstake', p_amount, p_solana_sig, p_reason);

    return true;
end;
$$ language plpgsql;

-- ----------------------------------------------------------------------------
-- 8. record_job_revenue_split — split the platform fee 50/30/20 and roll it
--    into the per-job columns and the global accounting singleton.
-- ----------------------------------------------------------------------------
create or replace function record_job_revenue_split(
    p_job_id uuid, p_total_cost_usdc numeric, p_provider_share_usdc numeric
)
returns void as $$
declare
    platform_fee     numeric;
    buyback_amount   numeric;
    treasury_amount  numeric;
    operations_amount numeric;
begin
    platform_fee     := p_total_cost_usdc - p_provider_share_usdc;
    buyback_amount   := platform_fee * 0.50;
    treasury_amount  := platform_fee * 0.30;
    operations_amount := platform_fee * 0.20;

    update jobs set
        buyback_budget_usdc = buyback_amount,
        treasury_usdc       = treasury_amount,
        operations_usdc     = operations_amount
    where id = p_job_id;

    update global_accounting set
        buyback_budget_usdc     = buyback_budget_usdc + buyback_amount,
        treasury_balance_usdc   = treasury_balance_usdc + treasury_amount,
        operations_balance_usdc = operations_balance_usdc + operations_amount,
        updated_at              = now()
    where id = 1;
end;
$$ language plpgsql;

-- ----------------------------------------------------------------------------
-- 9. record_buyback — log a completed swap and move budget -> held-for-burn.
--    Raises if the spend exceeds the accumulated buyback budget.
-- ----------------------------------------------------------------------------
create or replace function record_buyback(
    p_usdc_spent numeric,
    p_orvx_received numeric,
    p_solana_sig text,
    p_executor text,
    p_notes text default null
)
returns uuid as $$
declare
    event_id uuid;
    price    numeric;
begin
    if (select buyback_budget_usdc from global_accounting where id = 1) < p_usdc_spent then
        raise exception 'Insufficient buyback budget';
    end if;

    price := p_usdc_spent / p_orvx_received;

    insert into buyback_events
        (usdc_spent, orvx_received, execution_price_usdc_per_orvx, solana_signature, executed_by, notes)
    values
        (p_usdc_spent, p_orvx_received, price, p_solana_sig, p_executor, p_notes)
    returning id into event_id;

    update global_accounting set
        buyback_budget_usdc         = buyback_budget_usdc - p_usdc_spent,
        orvx_held_for_burn          = orvx_held_for_burn + p_orvx_received,
        total_orvx_bought           = total_orvx_bought + p_orvx_received,
        total_usdc_spent_on_buyback = total_usdc_spent_on_buyback + p_usdc_spent,
        updated_at                  = now()
    where id = 1;

    return event_id;
end;
$$ language plpgsql;

-- ----------------------------------------------------------------------------
-- 10. record_burn — log a burn and decrement held-for-burn.
--     Raises if the amount exceeds what's held for burn.
-- ----------------------------------------------------------------------------
create or replace function record_burn(
    p_orvx_burned numeric,
    p_solana_sig text,
    p_period_start timestamptz,
    p_period_end timestamptz,
    p_executor text,
    p_notes text default null
)
returns uuid as $$
declare
    event_id uuid;
begin
    if (select orvx_held_for_burn from global_accounting where id = 1) < p_orvx_burned then
        raise exception 'Insufficient ORVX held for burn';
    end if;

    insert into burn_events
        (orvx_burned, solana_signature, period_start, period_end, executed_by, notes)
    values
        (p_orvx_burned, p_solana_sig, p_period_start, p_period_end, p_executor, p_notes)
    returning id into event_id;

    update global_accounting set
        orvx_held_for_burn = orvx_held_for_burn - p_orvx_burned,
        total_orvx_burned  = total_orvx_burned + p_orvx_burned,
        updated_at         = now()
    where id = 1;

    return event_id;
end;
$$ language plpgsql;

-- ============================================================================
-- ROW LEVEL SECURITY
-- Match the rest of the schema: enable RLS, grant only service_role.
-- ============================================================================
alter table stakes            enable row level security;
alter table buyback_events    enable row level security;
alter table burn_events       enable row level security;
alter table global_accounting enable row level security;

do $$
declare t text;
begin
    foreach t in array array['stakes','buyback_events','burn_events','global_accounting']
    loop
        execute format('drop policy if exists service_role_all on %I', t);
        execute format(
            'create policy service_role_all on %I for all to service_role using (true) with check (true)',
            t
        );
    end loop;
end $$;

commit;
