-- ============================================================================
-- Orvix Orchestrator — migration 025: expand intel_scans scan_type constraint
-- Run AFTER 001-024. Idempotent. Applied via scripts/migrate.py.
--
-- Migration 022 created intel_scans with scan_type in (token, wallet,
-- accumulation). The intel system later added intelligence, social, and
-- top_holders scan types. This migration expands the check constraint to
-- include the new types.
-- ============================================================================

begin;

alter table intel_scans drop constraint if exists intel_scans_scan_type_check;
alter table intel_scans add constraint intel_scans_scan_type_check
    check (scan_type in ('token', 'wallet', 'accumulation', 'intelligence', 'social', 'top_holders'));

commit;
