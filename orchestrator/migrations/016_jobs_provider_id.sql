-- ============================================================================
-- Orvix Orchestrator — migration 016: record the provider on each job
-- Run AFTER 001-015. Idempotent.
--
-- `jobs` stores who ran the request (user_id) and which node served it
-- (node_id), but not who owned that node. The provider was only reachable by
-- joining jobs -> nodes -> provider_id, so deleting a node destroyed the
-- attribution for every job it had served — including the provider_earning_usdc
-- already recorded against them. Node rows are deleted routinely (a retired
-- machine, a cleanup), and after migration 015 that deletion nulls jobs.node_id
-- by design, which severs the last link.
--
-- `image_jobs` (migration 010) already stores provider_id directly. This brings
-- the older `jobs` table in line with it rather than inventing a new pattern.
--
-- NOTE on style: the column and the constraint are added in SEPARATE statements
-- on purpose. `alter table ... add column if not exists <col> references ...`
-- silently drops the REFERENCES clause when the column already exists — that is
-- exactly how jobs.node_id ended up with no foreign key for months (see
-- migration 015). Never combine the two.
-- ============================================================================

begin;

-- ----------------------------------------------------------------------------
-- 1. Column, then constraint — separately, for the reason above.
--    ON DELETE SET NULL matches image_jobs.provider_id: losing the user must
--    not delete the job history or the accounting attached to it.
-- ----------------------------------------------------------------------------
alter table jobs add column if not exists provider_id uuid;

do $$
begin
    if not exists (
        select 1
          from pg_constraint
         where conname = 'jobs_provider_id_fkey'
           and conrelid = 'jobs'::regclass
    ) then
        alter table jobs
            add constraint jobs_provider_id_fkey
            foreign key (provider_id) references users(id) on delete set null;
    end if;
end
$$;

-- ----------------------------------------------------------------------------
-- 2. Back-fill what is still resolvable. Jobs whose node row is already gone
--    cannot be recovered — their attribution was lost before this migration
--    existed, and this is a record of that, not a cause of it.
-- ----------------------------------------------------------------------------
update jobs
   set provider_id = n.provider_id
  from nodes n
 where jobs.node_id = n.id
   and jobs.provider_id is null;

-- ----------------------------------------------------------------------------
-- 3. Index for per-provider earnings queries, mirroring idx_image_jobs_user_id.
-- ----------------------------------------------------------------------------
create index if not exists idx_jobs_provider_id on jobs (provider_id);

commit;
