"""Orvix database migration runner.

Tracks applied migrations in the `schema_migrations` table (see
migrations/009_schema_migrations.sql) so applies are idempotent without relying
on manual discipline.

Usage:
  python scripts/migrate.py status              # applied vs pending
  python scripts/migrate.py up                  # apply all pending (prompts)
  python scripts/migrate.py up --target 011     # apply up to a version
  python scripts/migrate.py up --yes            # no confirmation prompt
  python scripts/migrate.py validate            # verify checksums of applied

Connection: set DATABASE_URL (or ORVIX_PG_URL) to the Postgres URI, e.g. the
Supabase session pooler. Each migration file manages its own transaction
(`begin;`/`commit;`); the runner records it in schema_migrations after a
successful apply.
"""

import argparse
import hashlib
import os
import re
import sys
import time
from pathlib import Path

import psycopg2

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_FILENAME_RE = re.compile(r"^(\d{3})_(.+)\.sql$")


def parse_migration_filename(filename: str) -> dict | None:
    m = _FILENAME_RE.match(filename)
    return {"version": m.group(1), "name": m.group(2)} if m else None


def compute_checksum(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def migration_files() -> list[tuple[Path, dict]]:
    out = []
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        parsed = parse_migration_filename(f.name)
        if parsed:
            out.append((f, parsed))
    return out


def get_connection():
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("ORVIX_PG_URL")
    if not dsn:
        sys.exit("ERROR: set DATABASE_URL (or ORVIX_PG_URL) to the Postgres URI")
    conn = psycopg2.connect(dsn, connect_timeout=20)
    conn.autocommit = True
    return conn


def get_applied(conn) -> dict | None:
    """Return {version: checksum} of applied migrations, or None if the tracking
    table doesn't exist yet (not bootstrapped)."""
    try:
        with conn.cursor() as cur:
            cur.execute("select version, checksum from schema_migrations")
            return {v: c for v, c in cur.fetchall()}
    except psycopg2.errors.UndefinedTable:
        return None


def cmd_status(args) -> int:
    conn = get_connection()
    applied = get_applied(conn)
    if applied is None:
        print("schema_migrations table not found — bootstrap first:")
        print("  apply migrations/009_schema_migrations.sql in the Supabase SQL Editor,")
        print("  then run: python scripts/migrate.py up")
        return 1
    print(f"\n{'Version':<9}{'Name':<26}{'Status'}")
    print("-" * 50)
    for _f, p in migration_files():
        mark = "applied" if p["version"] in applied else "pending"
        print(f"{p['version']:<9}{p['name']:<26}{mark}")
    return 0


def cmd_up(args) -> int:
    conn = get_connection()
    applied = get_applied(conn)
    if applied is None:
        print("schema_migrations table not found — apply migrations/009 in Supabase first, then re-run.")
        return 1

    pending = []
    for f, p in migration_files():
        if p["version"] in applied:
            continue
        if args.target and p["version"] > args.target:
            break
        pending.append((f, p))

    if not pending:
        print("No pending migrations.")
        return 0

    print(f"Will apply {len(pending)} migration(s):")
    for _f, p in pending:
        print(f"  {p['version']} {p['name']}")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        return 1

    for f, p in pending:
        content = f.read_text()
        checksum = compute_checksum(content)
        start = time.time()
        try:
            with conn.cursor() as cur:
                cur.execute(content)  # file manages its own begin;/commit;
            elapsed_ms = int((time.time() - start) * 1000)
            with conn.cursor() as cur:
                cur.execute(
                    "insert into schema_migrations (version, name, checksum, execution_time_ms, applied_by) "
                    "values (%s, %s, %s, %s, %s) on conflict (version) do nothing",
                    (p["version"], p["name"], checksum, elapsed_ms, "cli"),
                )
            print(f"OK  {p['version']} {p['name']} ({elapsed_ms}ms)")
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED {p['version']} {p['name']}: {exc}")
            return 1
    return 0


def cmd_validate(args) -> int:
    conn = get_connection()
    applied = get_applied(conn)
    if applied is None:
        print("schema_migrations table not found — nothing to validate.")
        return 1
    by_version = {p["version"]: (f, p) for f, p in migration_files()}
    ok = True
    for version, stored in sorted(applied.items()):
        entry = by_version.get(version)
        if not entry:
            print(f"WARN {version}: applied but no matching file on disk")
            ok = False
            continue
        f, p = entry
        current = compute_checksum(f.read_text())
        if current == stored:
            print(f"OK   {version} {p['name']}: checksum matches")
        else:
            print(f"FAIL {version} {p['name']}: CHECKSUM MISMATCH (file changed after apply)")
            ok = False
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Orvix migration runner")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Show applied vs pending migrations")
    p_up = sub.add_parser("up", help="Apply pending migrations")
    p_up.add_argument("--target", help="Apply up to this version (e.g. 011)")
    p_up.add_argument("--yes", action="store_true", help="Skip confirmation")
    sub.add_parser("validate", help="Verify checksums of applied migrations")

    args = parser.parse_args()
    handler = {"status": cmd_status, "up": cmd_up, "validate": cmd_validate}[args.command]
    sys.exit(handler(args))


if __name__ == "__main__":
    main()
