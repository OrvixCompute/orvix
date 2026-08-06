-- ============================================================================
-- Orvix Orchestrator — migration 015: repair the jobs.node_id foreign key
-- Run AFTER 001-014. Idempotent.
--
-- Migration 002 line 34 reads:
--     alter table jobs add column if not exists node_id uuid
--         references nodes(id) on delete set null;
--
-- but migration 001 had already created `node_id uuid`, so `add column if not
-- exists` was a no-op — and it took the REFERENCES clause down with it.
-- Postgres does not add a constraint that rides along on a skipped ADD COLUMN,
-- so the foreign key has never existed in any database built from this set.
--
-- Confirmed in production 2026-08-06, by behaviour rather than inspection:
-- deleting every row from `nodes` succeeded (so nothing was RESTRICTing it) and
-- left 24 `jobs` rows still pointing at the deleted ids (so nothing was SET
-- NULLing them). Both are impossible if the constraint were present.
--
-- Consequences this repairs: deleting a node silently orphans job rows, and
-- nothing stops a job from referencing a node that never existed.
-- ============================================================================

begin;

-- ----------------------------------------------------------------------------
-- 1. Clear references left dangling while the constraint was missing. This has
--    to happen before the constraint is added or the ALTER fails validation.
--    Note that `jobs` has no provider_id: node_id -> nodes.provider_id is the
--    only link from a job to whoever served it, so these rows have already lost
--    their attribution. Nulling them makes that explicit rather than leaving a
--    pointer to something that is gone.
-- ----------------------------------------------------------------------------
update jobs
   set node_id = null
 where node_id is not null
   and not exists (select 1 from nodes n where n.id = jobs.node_id);

-- ----------------------------------------------------------------------------
-- 2. Add the constraint for real. ADD CONSTRAINT has no IF NOT EXISTS, so guard
--    on the catalog to keep this file re-runnable.
-- ----------------------------------------------------------------------------
do $$
begin
    if not exists (
        select 1
          from pg_constraint
         where conname = 'jobs_node_id_fkey'
           and conrelid = 'jobs'::regclass
    ) then
        alter table jobs
            add constraint jobs_node_id_fkey
            foreign key (node_id) references nodes(id) on delete set null;
    end if;
end
$$;

-- ----------------------------------------------------------------------------
-- 3. Index the referencing column. Postgres indexes the referenced side
--    automatically but not this one, and ON DELETE SET NULL has to find the
--    referencing rows on every node deletion — which now happens routinely as
--    nodes come and go.
-- ----------------------------------------------------------------------------
create index if not exists idx_jobs_node_id on jobs (node_id);

commit;
