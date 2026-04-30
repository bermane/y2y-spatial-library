"""Migration 002 — relocate library contents under ``library/spatial/``.

Why
---
``schema.sql`` declares ``file_path`` as "relative to ``library/spatial/``"
so the catalogue can grow to multiple dataset types (e.g. tabular,
imagery products) by adding sibling library subtrees:

    library/
        spatial/
            <Category_Folders>/
                ...files...
        tabular/        # future
        imagery/        # future

Pre-migration the spatial files lived directly under ``library/<Category>/``;
this script moves them down a level into ``library/spatial/<Category>/``
without touching the ``file_path`` values in the catalogue. The
file_paths are already correct *relative to the new ``library/spatial/``
root* — only the absolute layout on disk changes.

Code-side, the ``library_root`` argument that ingest/reconcile/lifecycle
take changes from ``<root>/library`` to ``<root>/library/spatial``;
that update lives in :mod:`pipeline.__main__`'s ``_resolve_paths``.

Behaviour
---------
* **Idempotent.** Refuses to run if version ``'002'`` is already
  recorded in ``schema_migrations``, unless ``--force``. With
  ``--force`` it skips the move when ``library/spatial/`` already
  contains the expected layout.
* **Pre-flight checks.**
    1. Catalogue exists and migration 001 is applied (else nothing to
       restructure relative to).
    2. Every catalogue ``file_path`` resolves under the current
       ``library/<Category>/`` layout. If any is missing on disk, abort
       — let the steward run ``y2y reconcile`` first.
    3. ``library/spatial/`` either doesn't exist, or contains nothing
       that would collide with the move.
* **Move.** Each top-level entry under ``library/`` (the category
  folders, plus any rogue siblings the steward keeps there) is moved
  into ``library/spatial/`` with ``Path.rename``. ``library/.gitkeep``,
  ``library/README.md``, and the new ``library/spatial/`` itself are
  excluded from the move list.
* **Audit.** ``schema_migrations`` gets a row for version ``'002'``.
  No per-dataset changelog rows are written — file_paths are
  unchanged from the catalogue's point of view. The migration's row in
  ``schema_migrations`` is the audit record for this structural shift.

Usage
-----
::

    python pipeline/migrations/002_library_spatial_restructure.py
    python pipeline/migrations/002_library_spatial_restructure.py --force
"""

from __future__ import annotations

import argparse
import shutil
import sys
from contextlib import closing
from pathlib import Path

from pipeline import db as _db
from pipeline.utils import utc_now_iso

MIGRATION_VERSION = "002"
MIGRATION_DESCRIPTION = "Relocate spatial library under library/spatial/"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "inventory" / "inventory.db"
DEFAULT_LIBRARY = PROJECT_ROOT / "library"
SPATIAL_SUBDIR = "spatial"

# Top-level entries inside library/ that are *not* spatial-data folders
# and should be left in place during the move.
_LIBRARY_NON_DATA_ENTRIES: frozenset[str] = frozenset({
    ".gitkeep", "README.md", "spatial",
    ".DS_Store",
})


class MigrationError(RuntimeError):
    pass


def _migration_already_applied(conn) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?",
        (MIGRATION_VERSION,),
    )
    return cur.fetchone() is not None


def _check_001_applied(conn) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = '001'"
    )
    return cur.fetchone() is not None


def _list_catalogue_paths(conn) -> list[tuple[str, str]]:
    """Return [(dataset_id, file_path), …] for every active row."""
    cur = conn.execute(
        "SELECT dataset_id, file_path FROM datasets ORDER BY dataset_id"
    )
    return [(r[0], r[1]) for r in cur.fetchall()]


def _move_top_level_entries(library_root: Path, spatial_dir: Path) -> list[str]:
    """Move every spatial-data top-level entry under ``library/`` into ``spatial/``.

    Returns the list of names that were moved.
    """
    moved: list[str] = []
    spatial_dir.mkdir(parents=True, exist_ok=True)
    for entry in sorted(library_root.iterdir()):
        if entry.name in _LIBRARY_NON_DATA_ENTRIES:
            continue
        if entry.name.startswith("."):
            # Skip dotfiles other than .gitkeep (already excluded) — IDE
            # caches, lock files, etc.
            continue
        dest = spatial_dir / entry.name
        if dest.exists():
            raise MigrationError(
                f"destination already exists: {dest}. Resolve before re-running."
            )
        entry.rename(dest)
        moved.append(entry.name)
    return moved


def run(*, db_path: Path, library_root: Path, force: bool) -> int:
    if not db_path.exists():
        print(f"ERROR: catalogue not found at {db_path}.", file=sys.stderr)
        return 1
    if not library_root.exists():
        print(f"ERROR: library not found at {library_root}.", file=sys.stderr)
        return 1

    spatial_dir = library_root / SPATIAL_SUBDIR

    with closing(_db.get_connection(db_path)) as conn:
        if not _check_001_applied(conn):
            print(
                "ERROR: migration 001 has not been applied. "
                "Run pipeline/migrations/001_xlsx_to_sqlite.py first.",
                file=sys.stderr,
            )
            return 1

        if _migration_already_applied(conn) and not force:
            print(
                f"ERROR: migration {MIGRATION_VERSION} already applied. "
                f"Pass --force to re-run (idempotent move).",
                file=sys.stderr,
            )
            return 1

        catalogue_paths = _list_catalogue_paths(conn)

        # --- pre-flight ----------------------------------------------
        # Every active row's file must already live under one of the
        # current top-level library/<Category>/... paths. If not, the
        # filesystem and the catalogue are out of sync and reconcile
        # should be run first.
        already_under_spatial = spatial_dir.exists() and any(
            (spatial_dir / fp).exists() for _, fp in catalogue_paths
        )

        if already_under_spatial and not force:
            print(
                f"ERROR: {spatial_dir} already contains catalogue files. "
                f"This usually means migration 002 ran but its row in "
                f"schema_migrations didn't get written. Pass --force to "
                f"reconcile state and record the migration.",
                file=sys.stderr,
            )
            return 1

        if not already_under_spatial:
            missing: list[tuple[str, str]] = []
            for did, fp in catalogue_paths:
                if not (library_root / fp).exists():
                    missing.append((did, fp))
            if missing:
                print(
                    "ERROR: the following catalogue file_paths are missing "
                    "from disk:",
                    file=sys.stderr,
                )
                for did, fp in missing:
                    print(f"  {did}: {fp}", file=sys.stderr)
                print(
                    "Run `y2y reconcile` to investigate before relocating.",
                    file=sys.stderr,
                )
                return 1

        # --- move ----------------------------------------------------
        if already_under_spatial:
            print(f"Skipping move: {spatial_dir} already contains catalogue files.")
            moved: list[str] = []
        else:
            print(f"Moving top-level entries from {library_root} → {spatial_dir}")
            moved = _move_top_level_entries(library_root, spatial_dir)
            for name in moved:
                print(f"  moved: {name}/")

        # --- post-move verification ---------------------------------
        print("Verifying catalogue files now resolve under library/spatial/…")
        bad: list[tuple[str, str]] = []
        for did, fp in catalogue_paths:
            if not (spatial_dir / fp).exists():
                bad.append((did, fp))
        if bad:
            print(
                "ERROR: post-move verification failed — these paths "
                "don't exist relative to library/spatial/:",
                file=sys.stderr,
            )
            for did, fp in bad:
                print(f"  {did}: {fp}", file=sys.stderr)
            return 2
        print(f"  all {len(catalogue_paths)} catalogue paths verified OK")

        # --- record migration ---------------------------------------
        with conn:
            if _migration_already_applied(conn):
                # --force re-run: leave the existing row in place.
                pass
            else:
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at, description) "
                    "VALUES (?, ?, ?)",
                    (MIGRATION_VERSION, utc_now_iso(), MIGRATION_DESCRIPTION),
                )
                print(f"Recorded migration {MIGRATION_VERSION} in schema_migrations.")

    # --- nice-to-have housekeeping --------------------------------------
    gitkeep = spatial_dir / ".gitkeep"
    if not gitkeep.exists():
        gitkeep.touch()
        print(f"  created {gitkeep.relative_to(library_root)}")

    print()
    print("─" * 60)
    print(f"Migration {MIGRATION_VERSION} complete.")
    print(f"  library:    {library_root}")
    print(f"  spatial:    {spatial_dir}")
    print(f"  moved:      {len(moved)} top-level entries")
    print(f"  catalogue:  {len(catalogue_paths)} paths verified")
    print()
    print(
        "Reminder: pipeline/__main__.py's _resolve_paths must return\n"
        "library_root = <root>/library/spatial for the CLI to find files.\n"
        "(Phase 3d already does this in this branch.)"
    )
    print("─" * 60)
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="migration-002",
        description="Relocate spatial library under library/spatial/.",
    )
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-run / record migration even if state suggests it already happened.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return run(db_path=args.db, library_root=args.library, force=args.force)
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
