"""Migration 010 — 2026 mid-year typology revision.

A follow-up to the director-workshop typology (migration 006). This
revision:

Merge
-----
::

    Jurisdictional & Political Boundaries  ┐
    Land Designations & Tenure             ┴→ Boundaries, Tenure & Governance
        folders: Juris_Political_Boundaries + Land_Designations_Tenure
                 → Boundaries_Tenure_Governance

Renames
-------
::

    Climate Resilience  → Climate Change
        folder: Climate_Resilience → Climate_Change
    Human Dimensions    → Human Dimensions of Conservation
        folder: Human_Dimensions → Human_Dimensions_Conservation

New
---
::

    Demographics & Socioeconomic Data
        folder: Demographics_Socioeconomic  (empty; population demographics +
        socioeconomic data, split out of the old Human Dimensions category)

Net: 10 → 10 categories (−1 from the merge, +1 from Demographics).

What this migration does
------------------------
1. **Pre-flight.** Migrations 001–009 applied; not already applied;
   ``queue/processing/pending.xlsx`` absent or empty; every active
   row's ``category`` is one of the pre-revision names (catches a
   partial prior run).
2. **Filesystem** (tolerant — skips cleanly when a folder is already in
   the new shape or absent, as on a fresh install): rename the two
   simple folders, merge the two boundary/tenure folders into
   ``Boundaries_Tenure_Governance``, and create
   ``Demographics_Socioeconomic``.
3. **Schema rebuild** — SQLite can't ``ALTER`` a CHECK, so ``datasets``
   is rebuilt with the new 10-value CHECK, copying rows via a ``CASE``
   translation (merge + renames).
4. **file_path UPDATE** — prefix replacement for the four renamed/merged
   folders.
5. **Changelog** — one ``metadata`` row per affected dataset (category
   change, and a second row where the file_path prefix changed).
6. **Record itself** in ``schema_migrations`` as version ``'010'``.

The category **split** (population demographics / socioeconomic data
moving out of Human Dimensions) is not auto-applied to existing rows —
every old ``Human Dimensions`` row becomes ``Human Dimensions of
Conservation``; the steward re-files any that are really Demographics
via ``y2y update`` afterward. In practice the catalogue this runs
against is fresh or empty, so there is nothing to split.

Usage
-----
::

    python pipeline/migrations/010_typology_2026_midyear.py
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from pipeline import db as _db
from pipeline import inventory_manager
from pipeline.utils import utc_now_iso

MIGRATION_VERSION = "010"
MIGRATION_DESCRIPTION = (
    "2026 mid-year typology revision (merge boundaries/tenure, rename "
    "climate + human dimensions, add demographics)"
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "inventory" / "inventory.db"
DEFAULT_LIBRARY = PROJECT_ROOT / "library" / "spatial"
DEFAULT_PROCESSING = PROJECT_ROOT / "queue" / "processing"

# (old_category, new_category). Two old categories map to the same new
# one (the merge); the CASE in the rebuild handles the many-to-one.
_CATEGORY_REMAP: tuple[tuple[str, str], ...] = (
    ("Jurisdictional & Political Boundaries", "Boundaries, Tenure & Governance"),
    ("Land Designations & Tenure", "Boundaries, Tenure & Governance"),
    ("Climate Resilience", "Climate Change"),
    ("Human Dimensions", "Human Dimensions of Conservation"),
)

# Categories that survive the revision unchanged.
_UNCHANGED_CATEGORIES = frozenset({
    "Biodiversity & Ecosystems",
    "Connectivity & Wildlife Movement",
    "Species",
    "Water",
    "Land Cover, Land Use & Disturbance",
    "Threats & Infrastructure",
})

# (old_folder, new_folder). Both boundary/tenure folders target the
# same merged folder.
_FOLDER_REMAP: tuple[tuple[str, str], ...] = (
    ("Juris_Political_Boundaries", "Boundaries_Tenure_Governance"),
    ("Land_Designations_Tenure", "Boundaries_Tenure_Governance"),
    ("Climate_Resilience", "Climate_Change"),
    ("Human_Dimensions", "Human_Dimensions_Conservation"),
)

# New empty category folder to create post-rebuild.
_NEW_FOLDER = "Demographics_Socioeconomic"

# Full new column list for the rebuilt datasets table — matches
# pipeline/schema.sql (post-migration-008 shape: includes agol_format).
# Only the category CHECK differs from the pre-010 schema.
_NEW_DATASETS_DDL = """
CREATE TABLE datasets_new (
    dataset_id              TEXT PRIMARY KEY
                                CHECK (dataset_id LIKE 'ds_%'),
    dataset_type            TEXT NOT NULL DEFAULT 'spatial'
                                CHECK (dataset_type IN ('spatial')),
    title                   TEXT NOT NULL,
    category                TEXT NOT NULL
                                CHECK (category IN (
                                    'Boundaries, Tenure & Governance',
                                    'Biodiversity & Ecosystems',
                                    'Climate Change',
                                    'Connectivity & Wildlife Movement',
                                    'Species',
                                    'Water',
                                    'Land Cover, Land Use & Disturbance',
                                    'Human Dimensions of Conservation',
                                    'Threats & Infrastructure',
                                    'Demographics & Socioeconomic Data'
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
    agol_format             TEXT
                                CHECK (agol_format IS NULL OR agol_format IN (
                                    'feature-layer',
                                    'vector-tile-layer',
                                    'imagery-layer'
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

# Column list for the schema-rebuild copy. Order matches _NEW_DATASETS_DDL.
_DATASET_COLUMNS = (
    "dataset_id", "dataset_type", "title", "category", "subcategory",
    "file_path", "format",
    "summary", "description", "tags", "terms_of_use",
    "acknowledgements", "data_steward", "internal_notes",
    "status", "date_added", "date_modified",
    "agol_item_id", "agol_published_at", "last_synced_at", "sync_status",
    "agol_format",
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
    return conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?",
        (MIGRATION_VERSION,),
    ).fetchone() is not None


def _check_prereq_migrations(conn) -> list[str]:
    have = {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    return [v for v in ("001", "002", "003", "004", "005", "006", "007", "008", "009")
            if v not in have]


def _move_dir_contents(src: Path, dst: Path, moved: list[tuple[Path, Path]]) -> None:
    """Move every entry from ``src`` into ``dst`` (created if absent).

    Records each (from, to) in ``moved`` for rollback. Raises on a
    name collision rather than overwriting.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for entry in list(src.iterdir()):
        if entry.name == ".gitkeep":
            entry.unlink()  # redundant placeholder; dst has/gets its own
            continue
        target = dst / entry.name
        if target.exists():
            raise FileExistsError(
                f"merge collision: {target} already exists (from {entry})"
            )
        entry.rename(target)
        moved.append((target, entry))


def run(*, db_path: Path, library_root: Path, processing_dir: Path) -> int:
    if not db_path.exists():
        print(f"ERROR: catalogue not found at {db_path}", file=sys.stderr)
        return 1

    pending_path = processing_dir / "pending.xlsx"

    # --- pre-flight ----------------------------------------------------
    with closing(_db.get_connection(db_path)) as conn:
        if _migration_already_applied(conn):
            print(f"ERROR: migration {MIGRATION_VERSION} already applied.", file=sys.stderr)
            return 1
        missing = _check_prereq_migrations(conn)
        if missing:
            print(f"ERROR: prerequisite migrations not applied: {missing}.", file=sys.stderr)
            return 1

        pre_revision = {old for old, _ in _CATEGORY_REMAP} | _UNCHANGED_CATEGORIES
        unexpected = [
            (r["dataset_id"], r["category"])
            for r in conn.execute(
                "SELECT dataset_id, category FROM datasets WHERE status='active'"
            ).fetchall()
            if r["category"] not in pre_revision
        ]
        if unexpected:
            print(
                "ERROR: found active rows with categories outside the "
                "pre-revision set (likely a partial prior run):",
                file=sys.stderr,
            )
            for did, cat in unexpected:
                print(f"  {did}: {cat!r}", file=sys.stderr)
            return 1

        print("Pre-flight counts by category:")
        for r in conn.execute(
            "SELECT category, COUNT(*) AS n FROM datasets WHERE status='active' "
            "GROUP BY category ORDER BY category"
        ):
            print(f"  {r['n']:2d}  {r['category']}")

    if pending_path.exists():
        from pipeline import pending_sheet
        rows = pending_sheet.load_pending(pending_path)
        if rows:
            print(
                f"ERROR: {pending_path} has {len(rows)} in-flight row(s). "
                f"Approve or clear them before running this migration.",
                file=sys.stderr,
            )
            return 1

    print()
    print("Pre-flight OK. Reshaping folders, schema, and category values.")
    print()

    # --- filesystem (tolerant; track for rollback) --------------------
    # renamed: simple dir renames we can reverse.  moved: per-file moves
    # from the merge we can reverse.  created: dirs we made.
    renamed: list[tuple[Path, Path]] = []
    moved: list[tuple[Path, Path]] = []
    created: list[Path] = []
    try:
        # Simple renames: Climate_Resilience, Human_Dimensions.
        for old_folder, new_folder in (
            ("Climate_Resilience", "Climate_Change"),
            ("Human_Dimensions", "Human_Dimensions_Conservation"),
        ):
            old_dir = library_root / old_folder
            new_dir = library_root / new_folder
            if old_dir.is_dir() and not new_dir.exists():
                old_dir.rename(new_dir)
                renamed.append((new_dir, old_dir))
                print(f"  {old_folder}/  →  {new_folder}/")

        # Merge: Juris_Political_Boundaries + Land_Designations_Tenure
        # → Boundaries_Tenure_Governance.
        btg = library_root / "Boundaries_Tenure_Governance"
        made_btg = not btg.exists()
        for old_folder in ("Juris_Political_Boundaries", "Land_Designations_Tenure"):
            src = library_root / old_folder
            if src.is_dir():
                _move_dir_contents(src, btg, moved)
                # Remove the now-empty source folder.
                try:
                    src.rmdir()
                    print(f"  merged {old_folder}/  →  Boundaries_Tenure_Governance/")
                except OSError:
                    print(f"  merged {old_folder}/ contents; source not empty, left in place")
        if made_btg and btg.exists():
            (btg / ".gitkeep").touch()
            created.append(btg)

        # New empty category folder.
        demo = library_root / _NEW_FOLDER
        if not demo.exists():
            demo.mkdir(parents=True)
            (demo / ".gitkeep").touch()
            created.append(demo)
            print(f"  created {_NEW_FOLDER}/")
    except Exception as exc:
        print(f"ERROR: filesystem reshape failed: {exc}", file=sys.stderr)
        _rollback_fs(renamed, moved, created)
        return 2
    print()

    # --- schema rebuild + data updates (transactional) ----------------
    print("Rebuilding datasets table with new 10-category CHECK…")
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN")
            try:
                conn.execute(_NEW_DATASETS_DDL)

                case_sql = "\n                ".join(
                    f"WHEN '{old}' THEN '{new}'" for old, new in _CATEGORY_REMAP
                )
                select_exprs = []
                for c in _DATASET_COLUMNS:
                    if c == "category":
                        select_exprs.append(
                            f"CASE category\n                {case_sql}\n"
                            f"                ELSE category\n            END AS category"
                        )
                    else:
                        select_exprs.append(c)
                insert_cols = ", ".join(_DATASET_COLUMNS)
                select_sql = ",\n            ".join(select_exprs)
                conn.execute(
                    f"INSERT INTO datasets_new ({insert_cols})\n"
                    f"        SELECT\n            {select_sql}\n"
                    f"        FROM datasets"
                )

                conn.execute("DROP TABLE datasets")
                conn.execute("ALTER TABLE datasets_new RENAME TO datasets")
                for stmt in _NEW_INDEXES:
                    conn.execute(stmt)

                for old_folder, new_folder in _FOLDER_REMAP:
                    cur = conn.execute(
                        "UPDATE datasets SET file_path = REPLACE(file_path, ?, ?) "
                        "WHERE file_path LIKE ?",
                        (f"{old_folder}/", f"{new_folder}/", f"{old_folder}/%"),
                    )
                    if cur.rowcount:
                        print(f"  file_path: '{old_folder}/' → '{new_folder}/'  "
                              f"({cur.rowcount} row(s))")

                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError(
                        f"foreign_key_check reported {len(violations)} violation(s)"
                    )

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
        _rollback_fs(renamed, moved, created)
        return 2

    print("  schema rebuild OK.")
    print()

    # --- changelog per affected row -----------------------------------
    now = utc_now_iso()
    new_from_old = dict(_CATEGORY_REMAP)          # old → new
    folder_new_to_old: dict[str, list[str]] = {}
    for old_f, new_f in _FOLDER_REMAP:
        folder_new_to_old.setdefault(new_f, []).append(old_f)

    cat_n = fp_n = 0
    with closing(_db.get_connection(db_path)) as conn:
        for row in conn.execute(
            "SELECT dataset_id, category, file_path FROM datasets WHERE status='active'"
        ).fetchall():
            # Category changelog: we can't perfectly reconstruct which old
            # name a merged row had, so we record the new value with a note.
            new_cat = row["category"]
            if new_cat in set(new_from_old.values()):
                inventory_manager.append_changelog(
                    db_path, timestamp=now, action="metadata",
                    dataset_id=row["dataset_id"], actor="migration-010",
                    path=row["file_path"],
                    detail=f"2026 mid-year typology revision: category → {new_cat!r}",
                    field_changed="category", old_value=None, new_value=new_cat,
                )
                cat_n += 1
            fp = row["file_path"]
            for new_f, olds in folder_new_to_old.items():
                if fp.startswith(f"{new_f}/"):
                    inventory_manager.append_changelog(
                        db_path, timestamp=now, action="metadata",
                        dataset_id=row["dataset_id"], actor="migration-010",
                        path=fp,
                        detail=(f"typology revision: file_path folder → '{new_f}/' "
                                f"(from one of {olds})"),
                        field_changed="file_path", old_value=None, new_value=fp,
                    )
                    fp_n += 1
                    break

    print("─" * 60)
    print(f"Migration {MIGRATION_VERSION} complete.")
    print(f"  category changelog rows:  {cat_n}")
    print(f"  file_path changelog rows: {fp_n}")
    print(f"  new category folder:      {_NEW_FOLDER}/")
    print("─" * 60)
    return 0


def _rollback_fs(
    renamed: list[tuple[Path, Path]],
    moved: list[tuple[Path, Path]],
    created: list[Path],
) -> None:
    """Best-effort undo of the filesystem reshape after a later failure."""
    for target, original in reversed(moved):
        try:
            if target.exists() and not original.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                target.rename(original)
        except OSError:
            pass
    for new_dir, old_dir in reversed(renamed):
        try:
            if new_dir.exists() and not old_dir.exists():
                new_dir.rename(old_dir)
                print(f"  rolled back: {new_dir.name}/ → {old_dir.name}/")
        except OSError:
            pass
    for d in reversed(created):
        try:
            gk = d / ".gitkeep"
            if gk.exists():
                gk.unlink()
            d.rmdir()
        except OSError:
            pass


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="migration-010",
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
