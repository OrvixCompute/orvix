-- ============================================================================
-- Orvix Orchestrator — migration 008: stake-based tier system
-- Run AFTER 006/007. Idempotent.
--
-- Tier is now derived from users.staked_orvx (see app/services/tier_service.py).
-- The orchestrator computes tier at read time, but we keep the users.tier column
-- accurate for any direct-column consumers (dashboards, SQL reports) by:
--   1. back-filling it from current staked_orvx
--   2. a trigger that recomputes it whenever staked_orvx changes
-- Thresholds: bronze <10k, silver <50k, gold <250k, diamond >=250k.
-- ============================================================================

begin;

-- ----------------------------------------------------------------------------
-- Canonical tier-from-stake function (mirrors tier_service.tier_for_stake).
-- ----------------------------------------------------------------------------
create or replace function tier_from_stake(p_staked numeric)
returns text as $$
begin
    return case
        when p_staked >= 250000 then 'diamond'
        when p_staked >= 50000  then 'gold'
        when p_staked >= 10000  then 'silver'
        else 'bronze'
    end;
end;
$$ language plpgsql immutable;

-- ----------------------------------------------------------------------------
-- Back-fill: set every user's tier from their current stake.
-- ----------------------------------------------------------------------------
update users set tier = tier_from_stake(coalesce(staked_orvx, 0));

-- ----------------------------------------------------------------------------
-- Keep users.tier in sync whenever staked_orvx changes (insert or update).
-- ----------------------------------------------------------------------------
create or replace function sync_tier_from_stake() returns trigger as $$
begin
    new.tier := tier_from_stake(coalesce(new.staked_orvx, 0));
    return new;
end;
$$ language plpgsql;

drop trigger if exists trg_users_sync_tier on users;
create trigger trg_users_sync_tier
    before insert or update of staked_orvx on users
    for each row execute function sync_tier_from_stake();

-- ----------------------------------------------------------------------------
-- Seeded local test user: stake enough to retain gold tier (50k–250k).
-- ----------------------------------------------------------------------------
update users
    set staked_orvx = 50000
    where wallet_address = '5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9'
      and coalesce(staked_orvx, 0) < 50000;

commit;
