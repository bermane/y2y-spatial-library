"""Migration 012 — rename ``format`` / ``crs`` to ``library_format`` / ``library_crs``.

Why
---
Steward preference (2026-11-24): ``format`` next to ``source_format``
(and ``crs`` next to ``source_crs``) did not say which file each one
describes. The ``source_*`` columns describe the file as it arrived;
the renamed ``library_*`` columns describe the file as stored in the
library.

What this migration does
------------------------
1. Renames ``datasets.format`` → ``datasets.library_format`` and
   ``datasets.crs`` → ``datasets.library_crs`` via
   ``ALTER TABLE ... RENAME COLUMN`` (SQLite 3.25.0+). SQLite rewrites
   the reference inside the CHECK constraint itself.
2. Records itself in ``schema_migrations``.

No data changes: values are preserved column-for-column under the new
names. Old changelog entries that name ``crs`` in ``field_changed``
are left as written (the changelog is append-only).

Behaviour
---------
* **Idempotent.** Refuses to run if version ``'012'`` is already in
  ``schema_migrations``.
* **Fresh catalogues need nothing.** A catalogue created by this
  release (or later) already has the new names. The migration detects
  that, records itself, and changes nothing.
* **Partial states are refused.** If only one of the two columns has
  been renamed, the migration stops for a human to look.
* **No prerequisite check**, for the same reason as migration 011: a
  catalogue bootstrapped from ``schema.sql`` has an empty
  ``schema_migrations`` table. Run 011 as well on a pre-0.9.0
  catalogue; the order does not matter.

Usage
-----
Pause OneDrive sync first, then::

    python pipeline/migrations/012_rename_format_crs_to_library.py --db <path to inventory.db>
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from pipeline import db as _db
from pipeline.utils import utc_now_iso

MIGRATION_VERSION = "012"
MIGRATION_DESCRIPTION = (
    "Rename datasets.format -> library_format and datasets.crs -> library_crs"
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "inventory" / "inventory.db"

RENAMES: tuple[tuple[str, str], ...] = (
    ("format", "library_format"),
    ("crs", "library_crs"),
)


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(datasets)")}


def run(*, db_path: Path) -> int:
    if not db_path.exists():
        print(f"ERROR: catalogue not found at {db_path}", file=sys.stderr)
        return 1

    # Plain connection for the pre-flight: the package's own helpers
    # select the new column names, which a pre-012 catalogue lacks.
    with closing(sqlite3.connect(db_path)) as conn:
        try:
            applied = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (MIGRATION_VERSION,),
            ).fetchone()
        except sqlite3.Error as exc:
            print(f"ERROR: not a catalogue database: {exc}", file=sys.stderr)
            return 1
        if applied is not None:
            print(
                f"ERROR: migration {MIGRATION_VERSION} already applied.",
                file=sys.stderr,
            )
            return 1

        cols = _columns(conn)
        if not cols:
            print("ERROR: datasets table not found.", file=sys.stderr)
            return 1

    done = [new for _, new in RENAMES if new in cols]
    todo = [(old, new) for old, new in RENAMES if old in cols and new not in cols]
    if len(done) + len(todo) != len(RENAMES) or (done and todo):
        print(
            "ERROR: datasets is in an unexpected state (expected either "
            "format + crs, or library_format + library_crs). Investigate "
            "before re-running.",
            file=sys.stderr,
        )
        return 1

    if todo:
        for old, new in todo:
            print(f"Renaming datasets.{old} -> datasets.{new}")
    else:
        print("Columns already renamed; recording the migration only.")

    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN")
            try:
                for old, new in todo:
                    conn.execute(
                        f"ALTER TABLE datasets RENAME COLUMN {old} TO {new}"
                    )
                cols = _columns(conn)
                for old, new in RENAMES:
                    if new not in cols or old in cols:
                        raise RuntimeError(
                            f"rename {old} -> {new} did not take effect"
                        )
                fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if fk_violations:
                    raise RuntimeError(f"FK violations after rename: {fk_violations}")
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

    # Confirm the catalogue reads through the package's own helpers.
    with closing(_db.get_connection(db_path)) as conn:
        n_rows = conn.execute(
            "SELECT COUNT(*) FROM datasets WHERE library_format IS NOT NULL "
            "AND library_crs IS NOT NULL"
        ).fetchone()[0]
    print(f"OK: migration {MIGRATION_VERSION} applied ({n_rows} row(s)).")
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
