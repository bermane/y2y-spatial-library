"""Migration 007 — add ``agol_target`` column to ``datasets``.

Why
---
The AGOL integration (DESIGN.md §15) needs to know, per dataset,
whether the steward intends it to publish as a Hosted Feature Layer,
a Vector Tile Layer, or a Hosted Imagery Layer. The choice is
**persistent per dataset** so auto-sync (Phase C) can pick the right
publish path without per-invocation flags. Phase A adds the column,
Phase B reads it during ``push()``.

What this migration does
------------------------
1. Adds an ``agol_target`` column to ``datasets`` with a CHECK
   constraint enforcing the 3-value enum
   (``feature-layer`` / ``vector-tile-layer`` / ``imagery-layer``).
2. Backfills values: ``feature-layer`` for ``format='geopackage'``
   rows, ``imagery-layer`` for ``format='geotiff'`` rows. The
   steward overrides per-row later via ``y2y update``.
3. Recreates the four named indexes on the rebuilt table.
4. Verifies FK integrity (the ``changelog.dataset_id`` FK has to
   reconnect cleanly across the rebuild).
5. Records itself in ``schema_migrations``.

SQLite has no ``ALTER TABLE ... ADD COLUMN WITH CHECK`` for a CHECK
that references a new column on its own, so the only safe path is
the table-rebuild dance. This migration follows the same pattern as
migration 006 (typology revision):

    CREATE datasets_new (...new schema...);
    INSERT INTO datasets_new SELECT ... FROM datasets;
    DROP TABLE datasets;
    ALTER TABLE datasets_new RENAME TO datasets;

Wrapped in an explicit BEGIN/COMMIT/ROLLBACK so DDL is rolled back on
failure (CPython's sqlite3 only auto-transactions DML). Uses
``conn.execute()`` not ``executescript()`` so transactions aren't
auto-committed mid-rebuild.

Behaviour
---------
* **Idempotent.** Refuses to run if version ``'007'`` already in
  ``schema_migrations``. One-shot; no ``--force``.
* **Pre-flight.** Migrations 001–006 applied; every active row has a
  ``format`` in the expected legacy set so the backfill is safe.
* **Audit.** No per-row changelog entries — this is a schema-only
  change. The ``schema_migrations`` row is the audit record.

Usage
-----
::

    python pipeline/migrations/007_agol_target_column.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from pipeline import db as _db
from pipeline.utils import utc_now_iso

MIGRATION_VERSION = "007"
MIGRATION_DESCRIPTION = "Add agol_target column to datasets (enum, CHECK, backfill from format)"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "inventory" / "inventory.db"


# Full new column list for the rebuilt datasets table. Mirrors
# pipeline/schema.sql with one addition: agol_target. Keep this in
# lock-step with schema.sql after the migration runs.
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
    -- NEW in migration 007: AGOL publish-target intent. Nullable
    -- only because the catalogue may eventually grow dataset_types
    -- whose publish targets don't fit this enum; for current
    -- (dataset_type='spatial') rows, backfill guarantees non-null.
    agol_target             TEXT
                                CHECK (agol_target IS NULL OR agol_target IN (
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

# Old → new column copy + agol_target backfill via CASE on format.
# Column order in this list matches the new schema; agol_target sits
# alongside the other AGOL fields in the lifecycle/AGOL block.
_NEW_COLUMNS = (
    "dataset_id", "dataset_type", "title", "category", "subcategory",
    "file_path", "format",
    "summary", "description", "tags", "terms_of_use",
    "acknowledgements", "data_steward", "internal_notes",
    "status", "date_added", "date_modified",
    "agol_item_id", "agol_published_at", "last_synced_at", "sync_status",
    "agol_target",
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
    return [v for v in ("001", "002", "003", "004", "005", "006") if v not in have]


def run(*, db_path: Path) -> int:
    if not db_path.exists():
        print(f"ERROR: catalogue not found at {db_path}", file=sys.stderr)
        return 1

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

        # Confirm we don't already have agol_target on the existing
        # table (would indicate a partial prior run).
        cur = conn.execute("PRAGMA table_info(datasets)")
        existing_cols = {r[1] for r in cur.fetchall()}
        if "agol_target" in existing_cols:
            print(
                "ERROR: agol_target column already exists on datasets. "
                "Investigate before re-running.",
                file=sys.stderr,
            )
            return 1

        # Surface pre-rebuild counts for the steward.
        print("Pre-flight backfill plan:")
        for r in conn.execute(
            "SELECT format, COUNT(*) AS n FROM datasets "
            "GROUP BY format ORDER BY format"
        ):
            target = "feature-layer" if r["format"] == "geopackage" else "imagery-layer"
            print(f"  {r['n']:2d}  format={r['format']!r}  →  agol_target={target!r}")

    print()
    print("Rebuilding datasets table to add agol_target column…")

    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN")
            try:
                # 1. Create the new table.
                conn.execute(_NEW_DATASETS_DDL)

                # 2. Copy rows with the agol_target backfill CASE.
                insert_cols = ", ".join(_NEW_COLUMNS)
                select_exprs = []
                for c in _NEW_COLUMNS:
                    if c == "agol_target":
                        select_exprs.append(
                            "CASE format "
                            "WHEN 'geopackage' THEN 'feature-layer' "
                            "WHEN 'geotiff' THEN 'imagery-layer' "
                            "ELSE NULL "
                            "END AS agol_target"
                        )
                    else:
                        select_exprs.append(c)
                select_sql = ", ".join(select_exprs)
                conn.execute(
                    f"INSERT INTO datasets_new ({insert_cols}) "
                    f"SELECT {select_sql} FROM datasets"
                )

                # 3. Drop the original, rename the new one in.
                conn.execute("DROP TABLE datasets")
                conn.execute("ALTER TABLE datasets_new RENAME TO datasets")

                # 4. Recreate indexes.
                for stmt in _NEW_INDEXES:
                    conn.execute(stmt)

                # 5. Verify FK integrity.
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError(
                        f"foreign_key_check reported {len(violations)} violation(s) "
                        f"after rebuild — rolling back."
                    )

                # 6. Record migration.
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
        return 2

    # --- verify --------------------------------------------------------
    with closing(_db.get_connection(db_path)) as conn:
        print("\nPost-rebuild distribution:")
        for r in conn.execute(
            "SELECT format, agol_target, COUNT(*) AS n FROM datasets "
            "GROUP BY format, agol_target ORDER BY format, agol_target"
        ):
            print(f"  {r['n']:2d}  format={r['format']!r}  agol_target={r['agol_target']!r}")

    print()
    print("─" * 60)
    print(f"Migration {MIGRATION_VERSION} complete.")
    print("─" * 60)
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="migration-007",
        description="Add agol_target column to datasets (CHECK + backfill).",
    )
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    return run(db_path=args.db)


if __name__ == "__main__":
    raise SystemExit(main())
