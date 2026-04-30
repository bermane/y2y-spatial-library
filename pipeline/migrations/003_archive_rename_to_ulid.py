"""Migration 003 — rename queue/archived/ds_<hex>/ → ds_<ULID>/.

Why
---
Migration 001 re-keyed every catalogue row from ``ds_<12hex>`` to
``ds_<26-char ULID>``. The pre-existing per-dataset source bundles in
``queue/archived/`` were not renamed at the time, so the catalogue's
``dataset_id`` no longer resolves to the corresponding archive
directory.

This migration walks the ``id_format_migration`` rows in the
``changelog`` table — each one carries the
``old_value=ds_<hex>`` → ``new_value=ds_<ULID>`` mapping — and
renames the matching archive directory in place. The result: a
caller with a current ``dataset_id`` can find its source bundle at
``queue/archived/<dataset_id>/`` again, no special-case lookup
required.

Scope:

* Only directories whose name appears as ``old_value`` in an
  ``id_format_migration`` changelog row are touched.
* Archive directories whose name is already a ULID (Phase 6 ingest
  tests, anything ingested post-migration) are left alone.
* If both ``ds_<old>`` and the new ``ds_<ULID>`` directories already
  exist, the migration aborts and asks the steward to resolve.
* If neither exists, the row is silently skipped — that pre-migration
  dataset was probably ingested via the xlsx-era flow that didn't use
  archived bundles, so there's nothing on disk to rename.

Behaviour
---------
* **Idempotent.** Refuses to run if version ``'003'`` is already
  recorded in ``schema_migrations``, unless ``--force``. ``--force``
  re-attempts the rename of any directories still using the old
  format (no-op when there are none) and re-records the migration.
* **Audit.** Records itself in ``schema_migrations``. Per-dataset
  changelog rows are *not* written; the existing
  ``id_format_migration`` rows already capture the dataset_id move,
  and the archive rename is a one-time follow-on cleanup, not a
  catalogue mutation.

Usage
-----
::

    python pipeline/migrations/003_archive_rename_to_ulid.py
    python pipeline/migrations/003_archive_rename_to_ulid.py --force
"""

from __future__ import annotations

import argparse
import sys
from contextlib import closing
from pathlib import Path

from pipeline import db as _db
from pipeline.utils import utc_now_iso

MIGRATION_VERSION = "003"
MIGRATION_DESCRIPTION = "Rename queue/archived/ds_<hex>/ to ds_<ULID>/"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "inventory" / "inventory.db"
DEFAULT_ARCHIVED = PROJECT_ROOT / "queue" / "archived"


class MigrationError(RuntimeError):
    pass


def _migration_already_applied(conn) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?",
        (MIGRATION_VERSION,),
    )
    return cur.fetchone() is not None


def _id_format_mappings(conn) -> list[tuple[str, str]]:
    """Return [(old_id, new_id), …] from every id_format_migration row."""
    cur = conn.execute(
        "SELECT old_value, new_value FROM changelog "
        "WHERE action = 'id_format_migration' AND old_value IS NOT NULL "
        "AND new_value IS NOT NULL "
        "ORDER BY old_value"
    )
    return [(r[0], r[1]) for r in cur.fetchall()]


def run(*, db_path: Path, archived_dir: Path, force: bool) -> int:
    if not db_path.exists():
        print(f"ERROR: catalogue not found at {db_path}", file=sys.stderr)
        return 1
    if not archived_dir.exists():
        print(f"ERROR: archive root not found at {archived_dir}", file=sys.stderr)
        return 1

    with closing(_db.get_connection(db_path)) as conn:
        if _migration_already_applied(conn) and not force:
            print(
                f"ERROR: migration {MIGRATION_VERSION} already applied. "
                f"Pass --force to re-attempt.",
                file=sys.stderr,
            )
            return 1

        mappings = _id_format_mappings(conn)
        if not mappings:
            print(
                "ERROR: no id_format_migration rows found in changelog. "
                "Migration 001 must run first.",
                file=sys.stderr,
            )
            return 1

        print(f"Found {len(mappings)} old→new id mappings in changelog.")
        print(f"Inspecting {archived_dir}…")

        # --- pre-flight: report on each mapping --------------------
        plan: list[tuple[str, str]] = []        # (old, new) — to rename
        already_done: list[tuple[str, str]] = []  # already at new name
        absent: list[str] = []                   # neither old nor new on disk
        conflicts: list[tuple[str, str]] = []   # both on disk

        for old_id, new_id in mappings:
            old_dir = archived_dir / old_id
            new_dir = archived_dir / new_id
            old_exists = old_dir.is_dir()
            new_exists = new_dir.is_dir()

            if old_exists and new_exists:
                conflicts.append((old_id, new_id))
            elif old_exists:
                plan.append((old_id, new_id))
            elif new_exists:
                already_done.append((old_id, new_id))
            else:
                absent.append(old_id)

        if conflicts:
            print(
                f"ERROR: {len(conflicts)} archive(s) have both old and new "
                f"directory names — resolve manually before re-running:",
                file=sys.stderr,
            )
            for old_id, new_id in conflicts:
                print(f"  {old_id}/  AND  {new_id}/", file=sys.stderr)
            return 2

        print(f"  to rename:        {len(plan)}")
        print(f"  already renamed:  {len(already_done)}")
        print(f"  absent (skipped): {len(absent)}")

        # --- rename ----------------------------------------------
        for old_id, new_id in plan:
            (archived_dir / old_id).rename(archived_dir / new_id)
            print(f"  renamed: {old_id}/  →  {new_id}/")

        # --- record migration ------------------------------------
        with conn:
            if _migration_already_applied(conn):
                pass  # --force re-run; leave row in place
            else:
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at, description) "
                    "VALUES (?, ?, ?)",
                    (MIGRATION_VERSION, utc_now_iso(), MIGRATION_DESCRIPTION),
                )
                print(f"Recorded migration {MIGRATION_VERSION} in schema_migrations.")

    print()
    print("─" * 60)
    print(f"Migration {MIGRATION_VERSION} complete.")
    print(f"  archives renamed:    {len(plan)}")
    print(f"  already at new name: {len(already_done)}")
    print(f"  absent on disk:      {len(absent)}")
    print("─" * 60)
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="migration-003",
        description="Rename queue/archived/ds_<hex>/ → ds_<ULID>/ using id_format_migration mappings.",
    )
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--archived", type=Path, default=DEFAULT_ARCHIVED)
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-run / record migration even if it's already in schema_migrations.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return run(db_path=args.db, archived_dir=args.archived, force=args.force)
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
