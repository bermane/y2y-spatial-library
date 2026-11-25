"""Migration 011 — add the ``authoritative`` column to ``datasets``.

Why
---
The steward wants to mark, per dataset, whether it is the authoritative
copy. The library exists to hold authoritative datasets, so the column
defaults to yes and the steward sets no for the exceptions (for example
a third-party reference layer kept for convenience).

What this migration does
------------------------
1. Adds ``datasets.authoritative`` as
   ``INTEGER NOT NULL DEFAULT 1 CHECK (authoritative IN (0, 1))``
   (INTEGER boolean, DESIGN.md §13). Every existing row becomes
   ``1`` (yes).
2. Records itself in ``schema_migrations``.

No other data changes. In particular it does not rewrite ``tags``: the
"no tags" marker (``NA``) introduced in the same release only applies to
values the steward enters from now on.

Behaviour
---------
* **Idempotent.** Refuses to run if version ``'011'`` is already in
  ``schema_migrations``.
* **Fresh catalogues need nothing.** A catalogue created by this
  release (or later) already has the column from ``schema.sql``. The
  migration detects that, records itself, and changes nothing.
* **No prerequisite check.** Unlike 006-010 this migration does not
  rebuild the table, so it does not depend on the earlier ones having
  been recorded. A catalogue bootstrapped from ``schema.sql`` has an
  empty ``schema_migrations`` table and must not be refused.
* **Audit.** No per-row changelog entries; this is a schema-only
  change. The ``schema_migrations`` row is the audit record.

Usage
-----
Pause OneDrive sync first, then::

    python pipeline/migrations/011_authoritative_column.py --db <path to inventory.db>
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from pipeline import db as _db
from pipeline.utils import utc_now_iso

MIGRATION_VERSION = "011"
MIGRATION_DESCRIPTION = (
    "Add datasets.authoritative (INTEGER boolean, default 1 = yes)"
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "inventory" / "inventory.db"

_ADD_COLUMN_SQL = (
    "ALTER TABLE datasets ADD COLUMN authoritative INTEGER NOT NULL "
    "DEFAULT 1 CHECK (authoritative IN (0, 1))"
)


def run(*, db_path: Path) -> int:
    if not db_path.exists():
        print(f"ERROR: catalogue not found at {db_path}", file=sys.stderr)
        return 1

    with closing(_db.get_connection(db_path)) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (MIGRATION_VERSION,),
        ).fetchone()
        if applied is not None:
            print(
                f"ERROR: migration {MIGRATION_VERSION} already applied.",
                file=sys.stderr,
            )
            return 1

        cols = {r[1] for r in conn.execute("PRAGMA table_info(datasets)")}
        if not cols:
            print("ERROR: datasets table not found.", file=sys.stderr)
            return 1
        has_column = "authoritative" in cols
        n_rows = conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]

    if has_column:
        print("datasets.authoritative already exists; recording the migration only.")
    else:
        print(f"Adding datasets.authoritative ({n_rows} existing row(s) become yes)...")

    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("BEGIN")
            try:
                if not has_column:
                    conn.execute(_ADD_COLUMN_SQL)
                    cols = {
                        r[1] for r in conn.execute("PRAGMA table_info(datasets)")
                    }
                    if "authoritative" not in cols:
                        raise RuntimeError("column was not added")
                conn.execute(
                    "INSERT INTO schema_migrations "
                    "(version, description, applied_at) VALUES (?, ?, ?)",
                    (MIGRATION_VERSION, MIGRATION_DESCRIPTION, utc_now_iso()),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    except (sqlite3.Error, RuntimeError) as exc:
        print(f"ERROR: migration failed: {exc}", file=sys.stderr)
        return 1

    print(f"OK: migration {MIGRATION_VERSION} applied.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"Path to inventory.db (default: {DEFAULT_DB})",
    )
    args = parser.parse_args()
    return run(db_path=args.db)


if __name__ == "__main__":
    sys.exit(main())
