"""Migration 008 — rename ``agol_target`` column to ``agol_format``.

Why
---
Steward preference (2026-05-27): the column tells the pipeline
**what AGOL format** a dataset should publish as (Feature Layer,
Vector Tile Layer, Imagery Layer). The original ``agol_target``
name implied "AGOL endpoint" or "destination," which is
ambiguous; ``agol_format`` matches how the steward thinks about
the choice and groups visually with the other format-class
columns (``format``, ``source_format``).

What this migration does
------------------------
1. Renames ``datasets.agol_target`` → ``datasets.agol_format``
   via ``ALTER TABLE ... RENAME COLUMN`` (SQLite 3.25.0+).
2. SQLite auto-rewrites references inside CHECK constraints, so
   the enum constraint (``IN ('feature-layer',
   'vector-tile-layer', 'imagery-layer')``) carries over without
   special handling.
3. Records itself in ``schema_migrations``.

No data changes — values are preserved column-for-column under
the new name.

Behaviour
---------
* **Idempotent.** Refuses to run if version ``'008'`` already in
  ``schema_migrations``. One-shot; no ``--force``.
* **Pre-flight.** Migrations 001–007 applied; ``datasets`` must
  have ``agol_target`` and must NOT have ``agol_format`` (so we
  catch partial-run states).
* **Audit.** No per-row changelog entries — this is a schema-only
  change. The ``schema_migrations`` row is the audit record.

Usage
-----
::

    python pipeline/migrations/008_rename_agol_target_to_agol_format.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from pipeline import db as _db
from pipeline.utils import utc_now_iso

MIGRATION_VERSION = "008"
MIGRATION_DESCRIPTION = (
    "Rename datasets.agol_target → datasets.agol_format "
    "(naming alignment with format / source_format)"
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "inventory" / "inventory.db"


def _migration_already_applied(conn) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?",
        (MIGRATION_VERSION,),
    )
    return cur.fetchone() is not None


def _check_prereq_migrations(conn) -> list[str]:
    have = {
        r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
    }
    return [v for v in ("001", "002", "003", "004", "005", "006", "007")
            if v not in have]


def run(*, db_path: Path) -> int:
    if not db_path.exists():
        print(f"ERROR: catalogue not found at {db_path}", file=sys.stderr)
        return 1

    with closing(_db.get_connection(db_path)) as conn:
        if _migration_already_applied(conn):
            print(
                f"ERROR: migration {MIGRATION_VERSION} already applied.",
                file=sys.stderr,
            )
            return 1

        missing = _check_prereq_migrations(conn)
        if missing:
            print(
                f"ERROR: prerequisite migrations not applied: {missing}.",
                file=sys.stderr,
            )
            return 1

        # Column-state pre-flight: we should see agol_target but
        # not agol_format.
        cur = conn.execute("PRAGMA table_info(datasets)")
        existing_cols = {r[1] for r in cur.fetchall()}
        if "agol_target" not in existing_cols:
            print(
                "ERROR: datasets.agol_target not present (was migration "
                "007 applied?).",
                file=sys.stderr,
            )
            return 1
        if "agol_format" in existing_cols:
            print(
                "ERROR: datasets.agol_format already exists (partial "
                "prior run?). Investigate before re-running.",
                file=sys.stderr,
            )
            return 1

        # Row counts by current value, for steward visibility.
        print("Pre-flight: current agol_target value distribution:")
        for r in conn.execute(
            "SELECT agol_target, COUNT(*) AS n FROM datasets "
            "GROUP BY agol_target ORDER BY agol_target"
        ):
            print(f"  {r['n']:2d}  agol_target={r['agol_target']!r}")

    print()
    print("Renaming column datasets.agol_target → datasets.agol_format…")

    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN")
            try:
                # SQLite ALTER TABLE ... RENAME COLUMN rewrites
                # references inside CHECK / GENERATED / view bodies
                # automatically (3.25.0+).
                conn.execute(
                    "ALTER TABLE datasets "
                    "RENAME COLUMN agol_target TO agol_format"
                )

                # Verify the rename.
                cur = conn.execute("PRAGMA table_info(datasets)")
                cols = {r[1] for r in cur.fetchall()}
                if "agol_format" not in cols or "agol_target" in cols:
                    raise RuntimeError(
                        "rename did not produce expected column state"
                    )

                # FK integrity check.
                fk_violations = list(conn.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall())
                if fk_violations:
                    raise RuntimeError(
                        f"FK violations after rename: {fk_violations}"
                    )

                # Audit row.
                conn.execute(
                    "INSERT INTO schema_migrations "
                    "(version, description, applied_at) "
                    "VALUES (?, ?, ?)",
                    (MIGRATION_VERSION, MIGRATION_DESCRIPTION,
                     utc_now_iso()),
                )

                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.execute("PRAGMA foreign_keys = ON")
    except Exception as exc:
        print(f"ERROR: migration failed: {exc}", file=sys.stderr)
        return 1

    print()
    print(f"OK: migration {MIGRATION_VERSION} applied.")
    print("Post-rename agol_format value distribution:")
    with closing(_db.get_connection(db_path)) as conn:
        for r in conn.execute(
            "SELECT agol_format, COUNT(*) AS n FROM datasets "
            "GROUP BY agol_format ORDER BY agol_format"
        ):
            print(f"  {r['n']:2d}  agol_format={r['agol_format']!r}")
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
