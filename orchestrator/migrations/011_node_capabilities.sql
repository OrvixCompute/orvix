-- ============================================================================
-- Orvix Orchestrator — migration 011: node engine capabilities
-- Run AFTER 001-010. Idempotent. Applied via scripts/migrate.py.
--
-- Adds the engine list + total VRAM a node advertises at registration so the
-- orchestrator can route image jobs only to image-capable nodes.
-- ============================================================================

begin;

alter table nodes add column if not exists engines text[] default array['chat']::text[];
alter table nodes add column if not exists vram_gb numeric(6,1) default 0;

create index if not exists idx_nodes_engines on nodes using gin (engines);

commit;
