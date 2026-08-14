-- ============================================================================
-- Orvix Orchestrator — migration 021: OpenCovenant node attestation
-- Run AFTER 001-020. Idempotent (if not exists). Applied via scripts/migrate.py.
--
-- Adds the OpenCovenant attestation verdict to the nodes table. Written only
-- when the opt-in COVENANT_ENABLE_ATTESTATION flag is on; otherwise the column
-- stays null and the existing flow is untouched.
-- ============================================================================

begin;

alter table nodes add column if not exists covenant_attestation jsonb;

comment on column nodes.covenant_attestation is
    'OpenCovenant reputation verdict captured at node registration (opt-in). Null when attestation is disabled.';

commit;
