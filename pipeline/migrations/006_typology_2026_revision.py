"""Migration 006 — adopt the 2026 director-workshop typology revision.

The Y2Y directors and the steward updated the spatial-data typology
during a workshop. The new typology, captured in
``Spatial_Data_Typology.xlsx``, renames 4 categories, renames the
corresponding 3 folders (one of the renamed categories — Species —
keeps its folder name), and introduces 1 new category. Net: 9 → 10
categories.

Renames
-------
::

    Administrative & Jurisdictional Boundaries → Jurisdictional & Political Boundaries
        folder: Admin_Juris_Boundaries → Juris_Political_Boundaries
    Species & Species at Risk → Species
        folder: Species (unchanged)
    Protected Areas & Conservation Lands → Land Designations & Tenure
        folder: Prot_Areas_Cons_Lands → Land_Designations_Tenure
    Threats, Human Footprint & Infrastructure → Threats & Infrastructure
        folder: Threats_Human_Footprint_Infras → Threats_Infrastructure

New
---
::

    Human Dimensions
        folder: Human_Dimensions  (empty; no datasets yet)

What this migration does
------------------------
1. **Pre-flight.** Migrations 001–005 applied; ``queue/processing/
   pending.xlsx`` absent or empty; every active row's ``category``
   matches one of the legacy 9 names (catches a partially-applied
   prior run); every expected old-name library folder is present on
   disk.
2. **Filesystem renames** for the 3 folder-renamed categories under
   ``library/spatial/``. Done before the catalogue mutation so a
   schema-side abort leaves the catalogue and filesystem still
   in agreement on the *new* layout.
3. **Schema rebuild** — SQLite can't ``ALTER`` a CHECK constraint, so
   the migration drops/recreates ``datasets`` with the new 10-value
   CHECK, copying every row via an inline ``CASE`` translation of the
   4 renamed category values. FK enforcement is temporarily off
   (``PRAGMA foreign_keys = OFF``); ``foreign_key_check`` confirms
   integrity before re-enabling.
4. **file_path UPDATE** for the 3 folder-renamed categories'
   rows — prefix replacement only, every other path segment
   untouched.
5. **Index recreate** — DROP and CREATE the 4 indexes that were
   attached to the rebuilt table.
6. **Changelog rows** — one ``metadata`` row per affected dataset
   (14 of 17 rows). For the 7 rows that also got a ``file_path``
   change (2 + 4 + 1 = 7 in folder-renamed categories), a second
   ``metadata`` row records that diff.
7. **Create ``Human_Dimensions/``** with a ``.gitkeep`` so the
   directory shows up in git.
8. **Record itself** in ``schema_migrations`` as version ``'006'``.

One-shot; no ``--force``. The migration is data-aware (refuses if it
finds an already-renamed row, which would indicate a prior partial
apply) so re-running on a clean db is a clear "already applied"
error rather than silent corruption.

Usage
-----
::

    python pipeline/migrations/006_typology_2026_revision.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from pipeline import db as _db
from pipeline import inventory_manager
from pipeline.utils import utc_now_iso

MIGRATION_VERSION = "006"
MIGRATION_DESCRIPTION = "Adopt 2026 director-workshop typology revision (9→10 categories)"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "inventory" / "inventory.db"
DEFAULT_LIBRARY = PROJECT_ROOT / "library" / "spatial"
DEFAULT_PROCESSING = PROJECT_ROOT / "queue" / "processing"

# (old_category, new_category)
_CATEGORY_RENAMES: tuple[tuple[str, str], ...] = (
    ("Administrative & Jurisdictional Boundaries", "Jurisdictional & Political Boundaries"),
    ("Species & Species at Risk", "Species"),
    ("Protected Areas & Conservation Lands", "Land Designations & Tenure"),
    ("Threats, Human Footprint & Infrastructure", "Threats & Infrastructure"),
)

# (old_folder, new_folder) for the 3 folder renames. Species' folder
# is unchanged so it isn't here.
_FOLDER_RENAMES: tuple[tuple[str, str], ...] = (
    ("Admin_Juris_Boundaries", "Juris_Political_Boundaries"),
    ("Prot_Areas_Cons_Lands", "Land_Designations_Tenure"),
    ("Threats_Human_Footprint_Infras", "Threats_Infrastructure"),
)

# The new empty category folder to create post-rebuild.
_NEW_FOLDER = "Human_Dimensions"

# Full new column list for the rebuilt datasets table. Keep in lock
# step with pipeline/schema.sql; the new CHECK constraint is the only
# difference from the pre-006 schema.
#
# Created under the temp name datasets_new — SQLite's prescribed schema-
# change pattern is "CREATE new_X; INSERT INTO new_X; DROP X; RENAME
# new_X TO X". Renaming the original X first auto-rewrites foreign-key
# references in other tables to point at the renamed-out-of-the-way
# table, leaving them orphaned after a later DROP. The new-name-first
# order keeps the changelog.dataset_id FK pointing at "datasets"
# throughout, reconnecting cleanly when datasets_new is renamed.
_NEW_DATASETS_DDL = """
CREATE TABLE datasets_new (
    dataset_id              TEXT PRIMARY KEY
                                CHECK (dataset_id LIKE 'ds_%'),
    dataset_type            TEXT NOT NULL DEFAULT 'spatial'
                                CHECK (dataset_type IN ('spatial')),
    title                   TEXT NOT NULL,
    category                TEXT NOT NULL
                                CHECK (category IN (
                                    'Jurisdictional & Political Boundaries',
                                    'Land Designations & Tenure',
                                    'Biodiversity & Ecosystems',
                                    'Climate Resilience',
                                    'Connectivity & Wildlife Movement',
                                    'Species',
                                    'Water',
                                    'Land Cover, Land Use & Disturbance',
                                    'Human Dimensions',
                                    'Threats & Infrastructure'
                                )),
    subcategory             TEXT,
    file_path               TEXT NOT NULL,
    format                  TEXT NOT NULL
                                CHECK (format IN ('geopackage', 'geotiff')),
    summary                 TEXT NOT NULL,
    description             TEXT NOT NULL,
    tags                    TEXT NOT NULL,
    terms_of_use            TEXT NOT NULL,
    acknowledgements        TEXT NOT NULL,
    data_steward            TEXT NOT NULL,
    internal_notes          TEXT,
    status                  TEXT NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'deprecated', 'tombstoned')),
    date_added              TEXT NOT NULL,
    date_modified           TEXT NOT NULL,
    agol_item_id            TEXT,
    agol_published_at       TEXT,
    last_synced_at          TEXT,
    sync_status             TEXT NOT NULL DEFAULT 'unpublished'
                                CHECK (sync_status IN (
                                    'clean', 'pending_push', 'pending_pull',
                                    'conflict', 'error', 'unpublished'
                                )),
    checksum_sha256         TEXT NOT NULL,
    size_bytes              INTEGER NOT NULL,
    mtime                   TEXT NOT NULL,
    crs                     TEXT NOT NULL,
    geographic_extent_bbox  TEXT,
    classification          TEXT NOT NULL
                                CHECK (classification IN ('vector', 'continuous', 'categorical')),
    footprint_wkt           TEXT,
    temporal_start          TEXT,
    temporal_end            TEXT,
    feature_count           INTEGER,
    raster_width            INTEGER,
    raster_height           INTEGER,
    pixel_size_x            REAL,
    pixel_size_y            REAL,
    source_format           TEXT
                                CHECK (source_format IS NULL OR source_format IN (
                                    'shapefile', 'geopackage', 'geojson', 'kml', 'geotiff'
                                )),
    source_filename         TEXT,
    source_crs              TEXT,
    source_layer            TEXT
) STRICT;
"""

# Column list used for the schema-rebuild copy. Order matches
# _NEW_DATASETS_DDL.
_DATASET_COLUMNS = (
    "dataset_id", "dataset_type", "title", "category", "subcategory",
    "file_path", "format",
    "summary", "description", "tags", "terms_of_use",
    "acknowledgements", "data_steward", "internal_notes",
    "status", "date_added", "date_modified",
    "agol_item_id", "agol_published_at", "last_synced_at", "sync_status",
    "checksum_sha256", "size_bytes", "mtime", "crs",
    "geographic_extent_bbox", "classification",
    "footprint_wkt", "temporal_start", "temporal_end",
    "feature_count",
    "raster_width", "raster_height", "pixel_size_x", "pixel_size_y",
    "source_format", "source_filename", "source_crs", "source_layer",
)

_NEW_INDEXES = (
    "CREATE UNIQUE INDEX idx_datasets_agol_item_id_unique "
    "ON datasets(agol_item_id) WHERE agol_item_id IS NOT NULL",
    "CREATE INDEX idx_datasets_category ON datasets(category)",
    "CREATE INDEX idx_datasets_file_path ON datasets(file_path)",
    "CREATE INDEX idx_datasets_checksum_sha256 ON datasets(checksum_sha256)",
)


def _migration_already_applied(conn) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?",
        (MIGRATION_VERSION,),
    )
    return cur.fetchone() is not None


def _check_prereq_migrations(conn) -> list[str]:
    have = {
        r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    return [v for v in ("001", "002", "003", "004", "005") if v not in have]


def run(
    *,
    db_path: Path,
    library_root: Path,
    processing_dir: Path,
) -> int:
    if not db_path.exists():
        print(f"ERROR: catalogue not found at {db_path}", file=sys.stderr)
        return 1

    pending_path = processing_dir / "pending.xlsx"

    # --- pre-flight ----------------------------------------------------
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

        # Every active row's category must be one of the legacy 9. If we
        # see a new-name value here, a prior run partially applied.
        legacy_categories = {old for old, _ in _CATEGORY_RENAMES} | {
            "Biodiversity & Ecosystems", "Climate Resilience",
            "Connectivity & Wildlife Movement", "Water",
            "Land Cover, Land Use & Disturbance",
        }
        bad = conn.execute(
            "SELECT dataset_id, category FROM datasets WHERE status='active'"
        ).fetchall()
        unexpected = [
            (r["dataset_id"], r["category"]) for r in bad
            if r["category"] not in legacy_categories
        ]
        if unexpected:
            print(
                "ERROR: found active rows with categories outside the legacy "
                "9. Likely partial prior run — investigate before re-running.",
                file=sys.stderr,
            )
            for did, cat in unexpected:
                print(f"  {did}: {cat!r}", file=sys.stderr)
            return 1

        # Show the per-category counts so the steward sees what they're about
        # to mutate.
        print("Pre-flight counts by legacy category:")
        for r in conn.execute(
            "SELECT category, COUNT(*) AS n FROM datasets WHERE status='active' "
            "GROUP BY category ORDER BY category"
        ):
            print(f"  {r['n']:2d}  {r['category']}")

    if pending_path.exists():
        existing = inventory_manager  # avoid unused-import hint; not used here
        from pipeline import pending_sheet
        rows = pending_sheet.load_pending(pending_path)
        if rows:
            print(
                f"ERROR: {pending_path} has {len(rows)} in-flight row(s). "
                f"Approve or clear them before running this migration.",
                file=sys.stderr,
            )
            return 1

    # Folder pre-flight: every old folder must exist on disk; every new
    # folder name must NOT exist yet (so the rename has somewhere to land).
    for old_folder, new_folder in _FOLDER_RENAMES:
        old_dir = library_root / old_folder
        new_dir = library_root / new_folder
        if not old_dir.is_dir():
            print(
                f"ERROR: expected legacy folder missing: {old_dir}",
                file=sys.stderr,
            )
            return 1
        if new_dir.exists():
            print(
                f"ERROR: target folder already exists: {new_dir}",
                file=sys.stderr,
            )
            return 1

    print()
    print(f"Pre-flight OK. Renaming {len(_FOLDER_RENAMES)} folders, "
          f"reshaping schema, and translating category values.")
    print()

    # --- filesystem renames -------------------------------------------
    print(f"Renaming category folders under {library_root}/")
    renamed: list[tuple[Path, Path]] = []
    try:
        for old_folder, new_folder in _FOLDER_RENAMES:
            old_dir = library_root / old_folder
            new_dir = library_root / new_folder
            old_dir.rename(new_dir)
            renamed.append((old_dir, new_dir))
            print(f"  {old_folder}/  →  {new_folder}/")
    except OSError as exc:
        # Undo what we did and surface the error.
        print(f"ERROR: filesystem rename failed: {exc}", file=sys.stderr)
        for old_dir, new_dir in reversed(renamed):
            if new_dir.exists() and not old_dir.exists():
                new_dir.rename(old_dir)
                print(f"  rolled back: {new_dir.name}/  →  {old_dir.name}/")
        return 2
    print()

    # --- schema rebuild + data updates --------------------------------
    print("Rebuilding datasets table with new 10-category CHECK…")
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = OFF")
            # Explicit BEGIN — CPython's sqlite3 only opens an implicit
            # transaction before DML statements; DDL like CREATE/ALTER/
            # DROP/CREATE INDEX would otherwise run outside any
            # transaction and not be rolled back on failure. With an
            # explicit BEGIN, the whole rebuild is atomic — SQLite
            # itself supports rollback of DDL.
            conn.execute("BEGIN")
            try:
                # 1. Create the new table under a temp name.
                #
                # Use execute(), not executescript() — the latter
                # implicitly issues COMMIT before running the script,
                # defeating the transactional wrap.
                conn.execute(_NEW_DATASETS_DDL)

                # 2. Copy rows with inline category translation.
                case_sql = "\n                ".join(
                    f"WHEN '{old}' THEN '{new}'"
                    for old, new in _CATEGORY_RENAMES
                )
                insert_cols = ", ".join(_DATASET_COLUMNS)
                select_exprs = []
                for c in _DATASET_COLUMNS:
                    if c == "category":
                        select_exprs.append(
                            f"CASE category\n                {case_sql}\n                ELSE category\n            END AS category"
                        )
                    else:
                        select_exprs.append(c)
                select_sql = ",\n            ".join(select_exprs)
                conn.execute(
                    f"INSERT INTO datasets_new ({insert_cols})\n"
                    f"        SELECT\n            {select_sql}\n"
                    f"        FROM datasets"
                )

                # 3. Drop the original table, then rename the new one
                # into its place. Keeping the FK target name 'datasets'
                # stable across both ends of the rebuild lets
                # changelog.dataset_id reconnect cleanly.
                conn.execute("DROP TABLE datasets")
                conn.execute("ALTER TABLE datasets_new RENAME TO datasets")

                # 4. Recreate indexes (they were attached to the
                # original 'datasets' which we just dropped).
                for stmt in _NEW_INDEXES:
                    conn.execute(stmt)

                # 5. Update file_path prefixes for the 3 folder renames.
                for old_folder, new_folder in _FOLDER_RENAMES:
                    cur = conn.execute(
                        "UPDATE datasets SET file_path = "
                        "REPLACE(file_path, ?, ?) "
                        "WHERE file_path LIKE ?",
                        (f"{old_folder}/", f"{new_folder}/", f"{old_folder}/%"),
                    )
                    print(f"  file_path: '{old_folder}/' → '{new_folder}/'  "
                          f"({cur.rowcount} row(s) updated)")

                # 6. PRAGMA foreign_key_check while still inside the transaction.
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError(
                        f"foreign_key_check reported {len(violations)} violation(s) "
                        f"after rebuild — rolling back."
                    )

                # 7. Record migration in schema_migrations.
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at, description) "
                    "VALUES (?, ?, ?)",
                    (MIGRATION_VERSION, utc_now_iso(), MIGRATION_DESCRIPTION),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("PRAGMA foreign_keys = ON")
    except Exception as exc:
        print(f"ERROR: schema rebuild failed: {exc}", file=sys.stderr)
        # Roll back the filesystem renames so the user can re-run after fixing.
        for old_dir, new_dir in reversed(renamed):
            if new_dir.exists() and not old_dir.exists():
                new_dir.rename(old_dir)
                print(f"  rolled back: {new_dir.name}/  →  {old_dir.name}/")
        return 2

    print("  schema rebuild OK.")
    print()

    # --- write per-row changelog entries ------------------------------
    print("Writing changelog rows for affected datasets…")
    now = utc_now_iso()
    new_to_old = {new: old for old, new in _CATEGORY_RENAMES}
    folder_old_to_new = {old: new for old, new in _FOLDER_RENAMES}

    cat_changelog_count = 0
    fp_changelog_count = 0
    with closing(_db.get_connection(db_path)) as conn:
        # Find rows whose current (post-rename) category was a renamed
        # one. Distinguish by checking against new_to_old.
        rows = conn.execute(
            "SELECT dataset_id, category, file_path FROM datasets WHERE status='active'"
        ).fetchall()
        for row in rows:
            new_cat = row["category"]
            old_cat = new_to_old.get(new_cat)
            if old_cat is None:
                continue
            inventory_manager.append_changelog(
                db_path,
                timestamp=now,
                action="metadata",
                dataset_id=row["dataset_id"],
                actor="migration-006",
                path=row["file_path"],
                detail=(
                    f"typology revision: category {old_cat!r} → {new_cat!r}"
                ),
                field_changed="category",
                old_value=old_cat,
                new_value=new_cat,
            )
            cat_changelog_count += 1

            # If the file_path landed under a renamed folder, also log
            # that diff (reconstruct the old prefix from the new).
            fp = row["file_path"]
            for old_folder, new_folder in _FOLDER_RENAMES:
                prefix = f"{new_folder}/"
                if fp.startswith(prefix):
                    old_fp = f"{old_folder}/{fp[len(prefix):]}"
                    inventory_manager.append_changelog(
                        db_path,
                        timestamp=now,
                        action="metadata",
                        dataset_id=row["dataset_id"],
                        actor="migration-006",
                        path=fp,
                        detail=(
                            f"typology revision: file_path prefix "
                            f"'{old_folder}/' → '{new_folder}/'"
                        ),
                        field_changed="file_path",
                        old_value=old_fp,
                        new_value=fp,
                    )
                    fp_changelog_count += 1
                    break

    print(f"  wrote {cat_changelog_count} category-rename changelog rows")
    print(f"  wrote {fp_changelog_count} file_path-rename changelog rows")
    print()

    # --- create Human_Dimensions folder -------------------------------
    new_cat_dir = library_root / _NEW_FOLDER
    new_cat_dir.mkdir(exist_ok=True)
    (new_cat_dir / ".gitkeep").touch()
    print(f"Created new empty category folder: {new_cat_dir}/")
    print()

    # --- summary -------------------------------------------------------
    print("─" * 60)
    print(f"Migration {MIGRATION_VERSION} complete.")
    print(f"  category renames applied: {len(_CATEGORY_RENAMES)}")
    print(f"  folder renames applied:   {len(_FOLDER_RENAMES)}")
    print(f"  new category folder:      {_NEW_FOLDER}/")
    print(f"  changelog rows written:   {cat_changelog_count + fp_changelog_count}")
    print("─" * 60)
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="migration-006",
        description=(__doc__ or "").splitlines()[0],
    )
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    p.add_argument("--processing", type=Path, default=DEFAULT_PROCESSING)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    return run(
        db_path=args.db,
        library_root=args.library,
        processing_dir=args.processing,
    )


if __name__ == "__main__":
    raise SystemExit(main())
