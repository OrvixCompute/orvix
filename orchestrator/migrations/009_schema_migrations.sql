-- ============================================================================
-- Orvix Orchestrator — migration 009: schema migration tracking
-- Run AFTER 001-008. Idempotent. Bootstraps the migration-versioning table and
-- back-fills the 8 migrations that were applied manually before tracking existed.
--
-- BOOTSTRAP (one time): apply this file once in the Supabase SQL Editor. It
-- creates schema_migrations and seeds 001-008 (with their real SHA-256
-- checksums). Then run `python scripts/migrate.py up` once to register 009
-- itself (the re-run is a no-op thanks to IF NOT EXISTS / ON CONFLICT).
-- After that, all future migrations go through scripts/migrate.py.
--
-- Checksums = sha256 of each migration file's content (hashlib.sha256(text)).
-- ============================================================================

begin;

create table if not exists schema_migrations (
    version            text primary key,
    name               text not null,
    applied_at         timestamptz not null default now(),
    checksum           text not null,            -- sha256 of the migration file content
    execution_time_ms  integer,
    applied_by         text                       -- 'manual' | 'cli' | 'ci' | admin id
);

comment on table schema_migrations is 'Record of applied DB migrations (version, checksum, when, by whom).';

create index if not exists idx_schema_migrations_applied_at on schema_migrations (applied_at desc);

-- Back-fill migrations applied before tracking existed. ON CONFLICT keeps this
-- safe to re-run. applied_at for 001-005 is approximate (pre-tracking).
insert into schema_migrations (version, name, applied_at, checksum, applied_by) values
    ('001', 'initial_schema',      '2026-06-20 00:00:00+00', 'ab5236eba3cf9eb210ccc2379adf2cebcafca371a6eb98236c1cc9337d7a800f', 'manual'),
    ('002', 'nodes',               '2026-06-22 00:00:00+00', '73a9af97ae8a4207997a03564bca6a5866a0d8bfb9edf37d3bf76d83f19af40d', 'manual'),
    ('003', 'provider',            '2026-06-22 00:00:00+00', '4a7b5c589479c2045a920e4117d337ce3e36c45524eca3eb2dca413f497fc6b8', 'manual'),
    ('004', 'credit_topup',        '2026-06-23 00:00:00+00', '3692a1f8e3eb75fc94625a734909f6753927ef161b33a88037a2bbfd286b58a6', 'manual'),
    ('005', 'orvx_to_usdc',        '2026-06-23 00:00:00+00', '3f1710ce1d55b25413439384bfc37036520649d13f7d8f5c05c9adeb49eee6ce', 'manual'),
    ('006', 'staking_and_buyback', '2026-06-26 09:25:00+00', '7385e8b21a8f7d8b009f947159ef9cdbb2b80420cf27320075f19d2ea2377a5a', 'manual'),
    ('007', 'staking_intents',     '2026-06-26 09:25:00+00', 'e4afb5bd2bde59c3cb7cc50c3401c231751d6ed20583c9ef359e55c4ac5bd47e', 'manual'),
    ('008', 'tier_migration',      '2026-06-26 09:25:00+00', 'dbdd426a7f678bea17cc0d67f9767e571925e8d6199c0547fb8d22fe2cf90076', 'manual')
on conflict (version) do nothing;

-- RLS: service_role only, matching the rest of the schema.
alter table schema_migrations enable row level security;
drop policy if exists service_role_all on schema_migrations;
create policy service_role_all on schema_migrations for all to service_role using (true) with check (true);

commit;
