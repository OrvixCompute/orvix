# Database Migrations

Migrations are plain SQL files in `orchestrator/migrations/`, applied in order and
tracked in the `schema_migrations` table so applies are idempotent.

## Naming & conventions

- Filename: `NNN_name.sql` — zero-padded 3-digit version, lowercase
  `snake_case` name (e.g. `010_add_widgets.sql`). Enforced by CI
  (`.github/workflows/migration-order.yml`): named, **sequential (no gaps)**, unique.
- **Each file manages its own transaction** with `begin;` … `commit;`.
- Make every statement **idempotent** (`create table if not exists`,
  `alter table ... add column if not exists`, `insert ... on conflict do nothing`,
  `create or replace function`). This makes re-runs safe.
- Enable RLS on new tables with the `service_role_all` policy, matching the rest
  of the schema.

## Creating a new migration

1. Copy the next version number (e.g. `010`) and pick a name.
2. Write idempotent SQL wrapped in `begin;`/`commit;`.
3. Open a PR — CI checks the naming/order. Get it reviewed.
4. Apply it with the runner (below).

## Running migrations (`scripts/migrate.py`)

Needs a direct Postgres connection. Set `DATABASE_URL` (or `ORVIX_PG_URL`) — e.g.
the Supabase **session pooler** URI
(`postgresql://postgres.<ref>:<pw>@aws-...pooler.supabase.com:5432/postgres`).

```bash
export DATABASE_URL='postgresql://...'
python scripts/migrate.py status      # applied vs pending
python scripts/migrate.py up          # apply pending (prompts to confirm)
python scripts/migrate.py up --yes    # no prompt (used by deploy.sh)
python scripts/migrate.py up --target 011
python scripts/migrate.py validate    # checksums of applied vs files on disk
```

`up` runs each pending file, then records `version, name, checksum (sha256 of file
content), execution_time_ms, applied_by` in `schema_migrations`.

## Bootstrap (one time)

The tracking table was added after 001–008 had already been applied manually, so
it must be bootstrapped once:

1. Apply **`migrations/009_schema_migrations.sql`** in the Supabase SQL Editor.
   This creates `schema_migrations` and back-fills 001–008 with their real
   checksums.
2. Run `python scripts/migrate.py up` once. It sees 009 as the only "pending"
   entry, re-runs it (a no-op — `create table if not exists` /
   `on conflict do nothing`), and records `009` itself.
3. `python scripts/migrate.py validate` should now report all checksums OK.

From then on, every new migration goes through `migrate.py up`.

## Checksum integrity

`validate` recomputes `sha256(file content)` and compares to what was stored at
apply time. A **mismatch means a migration file was edited after it was applied** —
never do that; write a new corrective migration instead.

## Rollback

There is **no automated down-migration**. To revert, write a new forward
migration that undoes the change (e.g. `011_drop_widgets.sql`). Keep migrations
small so a corrective one is easy to reason about.
