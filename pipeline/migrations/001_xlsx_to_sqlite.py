"""Migration 001 — bootstrap inventory.db from inventory.xlsx.

One-shot migration that converts the legacy xlsx-as-source-of-truth
catalogue into the SQLite catalogue defined in ``pipeline/schema.sql``.

Behaviours
----------
* **Idempotent.** Refuses to run if version ``'001'`` is already
  recorded in ``schema_migrations`` unless ``--force`` is passed.
  ``--force`` wipes the destination ``.db`` and starts over; the
  pre-migration xlsx backup is left untouched.

* **ULID re-keying.** Every existing ``ds_<12 hex>`` ID is replaced by a
  fresh ``ds_<26-char ULID>``. Two changelog rows are written per
  dataset:

    1. ``migrated_from_xlsx`` — captures the row-level move from xlsx
       into the SQLite catalogue (no field diff; row-level audit).
    2. ``id_format_migration`` — captures the old→new ID mapping in
       ``old_value`` / ``new_value`` so the legacy ID can be traced
       forward forever.

* **Field rename / lowercase.** ``notes`` → ``internal_notes``;
  ``format`` and ``source_format`` lowercased to match the schema's
  CHECK constraints (``'GeoPackage'`` → ``'geopackage'``,
  ``'Cloud Optimized GeoTIFF'`` → ``'geotiff'``,
  ``'Shapefile'`` → ``'shapefile'``, ``'GeoTIFF'`` → ``'geotiff'``).
  The xlsx-era literal string ``'None'`` (Excel's stringified None) is
  treated as SQL NULL for nullable columns.

* **Date upgrade.** ``date_added`` / ``date_modified`` come out of the
  xlsx as bare ``YYYY-MM-DD`` strings; rewritten as
  ``YYYY-MM-DDT00:00:00Z`` to satisfy the ISO-8601-with-Z convention.

* **Type-agnostic defaults.** ``dataset_type='spatial'``,
  ``sync_status='unpublished'`` (per schema defaults).

* **Backfill.** Reads each canonical file from disk to populate the
  spatial-specific columns the xlsx never had:

    - ``footprint_wkt`` — canonical bbox reprojected to EPSG:4326,
      written as a 4-corner POLYGON WKT.
    - ``feature_count`` (vectors) — via fiona.
    - ``raster_width`` / ``raster_height`` / ``pixel_size_x`` /
      ``pixel_size_y`` (rasters) — via rasterio.

  Files that are missing or unreadable cause the migration to abort
  *before* writing — there is no partial-success state.

* **Validation.** After insertion: row count matches xlsx, every
  changelog FK resolves, no duplicate dataset_ids, schema_migrations
  has version 001. Any failure rolls the whole migration back (the
  ``.db`` file is removed) so re-running without ``--force`` will
  detect a clean slate.

* **Backup.** ``inventory.xlsx`` is renamed to
  ``inventory.xlsx.pre-migration-backup`` only after every other step
  succeeds. The old changelog.md is left in place for the steward to
  read; it is not consumed by the migration.

Usage
-----
::

    python pipeline/migrations/001_xlsx_to_sqlite.py
    python pipeline/migrations/001_xlsx_to_sqlite.py --force
    python pipeline/migrations/001_xlsx_to_sqlite.py \\
        --xlsx inventory/inventory.xlsx \\
        --db   inventory/inventory.db \\
        --library library

Run from the project root.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import fiona
import rasterio
import ulid
from pyproj import Transformer

# We deliberately avoid `from pipeline.migrations.001_...` style imports
# elsewhere — the module name starts with a digit and isn't importable.
# This script is run as a file path, so its own imports work fine.
from pipeline import inventory_manager
from pipeline.utils import utc_now_iso

MIGRATION_VERSION = "001"
MIGRATION_DESCRIPTION = "Bootstrap SQLite catalogue from inventory.xlsx"
MIGRATION_ACTOR = "migration-001"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = PROJECT_ROOT / "pipeline" / "schema.sql"

DEFAULT_XLSX = PROJECT_ROOT / "inventory" / "inventory.xlsx"
DEFAULT_DB = PROJECT_ROOT / "inventory" / "inventory.db"
# Post-migration 002 the spatial library lives under library/spatial/.
# Both layouts are tolerated by the CLI ``--library`` flag; this default
# matches the post-002 layout so a fresh re-run after restructure works.
DEFAULT_LIBRARY = PROJECT_ROOT / "library" / "spatial"
BACKUP_SUFFIX = ".pre-migration-backup"

# xlsx-era display-name → schema CHECK lowercase value.
FORMAT_MAP: dict[str, str] = {
    "GeoPackage": "geopackage",
    "Cloud Optimized GeoTIFF": "geotiff",
    "GeoTIFF": "geotiff",
}
SOURCE_FORMAT_MAP: dict[str, str] = {
    "Shapefile": "shapefile",
    "GeoPackage": "geopackage",
    "GeoJSON": "geojson",
    "KML": "kml",
    "GeoTIFF": "geotiff",
    "Cloud Optimized GeoTIFF": "geotiff",
}

# Canonical project CRS (matches lifecycle/ingest convention).
CANONICAL_CRS = "ESRI:102008"

# Columns that get inserted into `datasets`. Order matches schema.sql.
DATASET_COLUMNS: tuple[str, ...] = (
    "dataset_id",
    "dataset_type",
    "title",
    "category",
    "subcategory",
    "file_path",
    "format",
    "summary",
    "description",
    "tags",
    "terms_of_use",
    "acknowledgements",
    "data_steward",
    "internal_notes",
    "status",
    "date_added",
    "date_modified",
    "agol_item_id",
    "agol_published_at",
    "last_synced_at",
    "sync_status",
    "checksum_sha256",
    "size_bytes",
    "mtime",
    "crs",
    "geographic_extent_bbox",
    "classification",
    "footprint_wkt",
    "temporal_start",
    "temporal_end",
    "feature_count",
    "raster_width",
    "raster_height",
    "pixel_size_x",
    "pixel_size_y",
    "source_format",
    "source_filename",
    "source_crs",
    "source_layer",
)


class MigrationError(RuntimeError):
    """Aborts the migration; caller removes the partial .db file."""


# --- helpers ---------------------------------------------------------------

def _norm(v: Any) -> Any:
    """xlsx-era ``'None'`` / ``''`` → SQL NULL; otherwise pass through."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s == "" or s == "None":
            return None
        return s
    return v


def _upgrade_date(d: str | None) -> str | None:
    """``2026-04-27`` → ``2026-04-27T00:00:00Z``. Pass through if already ISO."""
    if d is None:
        return None
    s = str(d).strip()
    if not s:
        return None
    if "T" in s:
        # Already a full timestamp; ensure 'Z' suffix.
        return s if s.endswith("Z") else s + "Z"
    return f"{s}T00:00:00Z"


def _new_dataset_id() -> str:
    return f"ds_{ulid.ULID()}"


def _new_changelog_id() -> str:
    return f"cl_{ulid.ULID()}"


def _bbox_to_footprint_wkt(bbox_str: str, source_crs: str = CANONICAL_CRS) -> str | None:
    """Reproject canonical-CRS bbox to EPSG:4326, return as POLYGON WKT.

    Uses ``Transformer.transform_bounds`` with densification so the 4326
    bounding rectangle properly contains the original projected bbox
    (handles meridian/curvature distortion at high latitudes).
    """
    if not bbox_str:
        return None
    parts = [p.strip() for p in bbox_str.split(",")]
    if len(parts) != 4:
        return None
    try:
        minx, miny, maxx, maxy = (float(p) for p in parts)
    except ValueError:
        return None
    tf = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
    lon_min, lat_min, lon_max, lat_max = tf.transform_bounds(
        minx, miny, maxx, maxy, densify_pts=21
    )
    # 4-corner polygon in EPSG:4326 (lon lat order, CCW, explicit close).
    return (
        "POLYGON(("
        f"{lon_min:.6f} {lat_min:.6f}, "
        f"{lon_max:.6f} {lat_min:.6f}, "
        f"{lon_max:.6f} {lat_max:.6f}, "
        f"{lon_min:.6f} {lat_max:.6f}, "
        f"{lon_min:.6f} {lat_min:.6f}"
        "))"
    )


def _backfill_vector(path: Path) -> dict[str, Any]:
    with fiona.open(path) as src:
        return {"feature_count": len(src)}


def _backfill_raster(path: Path) -> dict[str, Any]:
    with rasterio.open(path) as ds:
        # pixel_size_y is reported as positive (north-up). Transform[4]
        # is typically negative for north-up rasters.
        psx = float(ds.transform.a)
        psy = float(-ds.transform.e)
        return {
            "raster_width": int(ds.width),
            "raster_height": int(ds.height),
            "pixel_size_x": psx,
            "pixel_size_y": psy,
        }


def _build_dataset_record(
    xlsx_row: dict[str, Any],
    new_id: str,
    library_root: Path,
) -> dict[str, Any]:
    """Map one xlsx row → one full datasets-table row dict."""
    fp = _norm(xlsx_row.get("file_path"))
    if not fp:
        raise MigrationError(f"row {xlsx_row.get('dataset_id')!r} has no file_path")

    full_path = library_root / fp
    if not full_path.exists():
        raise MigrationError(
            f"row {xlsx_row.get('dataset_id')!r}: file not found at {full_path}"
        )

    raw_format = _norm(xlsx_row.get("format"))
    if raw_format not in FORMAT_MAP:
        raise MigrationError(
            f"row {xlsx_row.get('dataset_id')!r}: unmapped format {raw_format!r}"
        )
    fmt = FORMAT_MAP[raw_format]

    raw_source_format = _norm(xlsx_row.get("source_format"))
    src_fmt: str | None = None
    if raw_source_format is not None:
        if raw_source_format not in SOURCE_FORMAT_MAP:
            raise MigrationError(
                f"row {xlsx_row.get('dataset_id')!r}: "
                f"unmapped source_format {raw_source_format!r}"
            )
        src_fmt = SOURCE_FORMAT_MAP[raw_source_format]

    classification = _norm(xlsx_row.get("classification"))
    if classification not in ("vector", "continuous", "categorical"):
        raise MigrationError(
            f"row {xlsx_row.get('dataset_id')!r}: "
            f"invalid classification {classification!r}"
        )

    bbox = _norm(xlsx_row.get("geographic_extent_bbox"))
    footprint_wkt = _bbox_to_footprint_wkt(bbox) if bbox else None

    record: dict[str, Any] = {
        # identity & taxonomy
        "dataset_id": new_id,
        "dataset_type": "spatial",
        "title": _norm(xlsx_row.get("title")),
        "category": _norm(xlsx_row.get("category")),
        "subcategory": _norm(xlsx_row.get("subcategory")),
        "file_path": fp,
        "format": fmt,
        # extrinsic metadata
        "summary": _norm(xlsx_row.get("summary")),
        "description": _norm(xlsx_row.get("description")),
        "tags": _norm(xlsx_row.get("tags")),
        "terms_of_use": _norm(xlsx_row.get("terms_of_use")),
        "acknowledgements": _norm(xlsx_row.get("acknowledgements")),
        "data_steward": _norm(xlsx_row.get("data_steward")),
        "internal_notes": _norm(xlsx_row.get("notes")),  # renamed
        # lifecycle
        "status": _norm(xlsx_row.get("status")) or "active",
        "date_added": _upgrade_date(_norm(xlsx_row.get("date_added"))),
        "date_modified": _upgrade_date(_norm(xlsx_row.get("date_modified"))),
        # AGOL (reserved — populate item_id if xlsx had one, leave the
        # rest null per DESIGN.md AGOL-integration placeholder).
        "agol_item_id": _norm(xlsx_row.get("agol_item_id")),
        "agol_published_at": None,
        "last_synced_at": None,
        "sync_status": "unpublished",
        # intrinsic snapshot
        "checksum_sha256": _norm(xlsx_row.get("checksum_sha256")),
        "size_bytes": int(xlsx_row["size_bytes"]) if xlsx_row.get("size_bytes") else None,
        "mtime": _norm(xlsx_row.get("mtime")),
        "crs": _norm(xlsx_row.get("crs")),
        "geographic_extent_bbox": bbox,
        "classification": classification,
        # spatial properties
        "footprint_wkt": footprint_wkt,
        "temporal_start": None,
        "temporal_end": None,
        "feature_count": None,
        "raster_width": None,
        "raster_height": None,
        "pixel_size_x": None,
        "pixel_size_y": None,
        # source provenance
        "source_format": src_fmt,
        "source_filename": _norm(xlsx_row.get("source_filename")),
        "source_crs": _norm(xlsx_row.get("source_crs")),
        "source_layer": _norm(xlsx_row.get("source_layer")),
    }

    # backfill spatial dims from disk
    if classification == "vector":
        record.update(_backfill_vector(full_path))
    else:
        record.update(_backfill_raster(full_path))

    # Required NOT NULLs sanity-check before we even touch the DB.
    required = (
        "title", "category", "file_path", "format",
        "summary", "description", "tags", "terms_of_use",
        "acknowledgements", "data_steward",
        "date_added", "date_modified",
        "checksum_sha256", "size_bytes", "mtime", "crs",
        "classification",
    )
    for col in required:
        if record[col] in (None, ""):
            raise MigrationError(
                f"row {xlsx_row.get('dataset_id')!r}: required column "
                f"{col!r} is empty after mapping"
            )

    return record


def _connect(db_path: Path) -> sqlite3.Connection:
    """SQLite connection with FKs on. (db.py will subsume this in Phase 3.)"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _apply_schema(conn: sqlite3.Connection) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)


def _check_already_applied(db_path: Path) -> bool:
    if not db_path.exists():
        return False
    with closing(_connect(db_path)) as conn:
        try:
            cur = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (MIGRATION_VERSION,),
            )
            return cur.fetchone() is not None
        except sqlite3.OperationalError:
            # Table doesn't exist yet → not applied.
            return False


# --- main ------------------------------------------------------------------

def run(
    *,
    xlsx_path: Path,
    db_path: Path,
    library_root: Path,
    force: bool,
) -> int:
    if not xlsx_path.exists():
        # Allow re-running after the xlsx was renamed to the backup, but
        # only if --force was *not* given (force without xlsx is meaningless).
        backup = xlsx_path.with_suffix(xlsx_path.suffix + BACKUP_SUFFIX)
        if backup.exists():
            print(
                f"NOTE: {xlsx_path.name} not found, but backup exists at "
                f"{backup.name}. The migration likely already ran. "
                f"Use --force to redo from the backup.",
                file=sys.stderr,
            )
            if force:
                xlsx_path = backup
            else:
                return 1
        else:
            print(f"ERROR: xlsx not found at {xlsx_path}", file=sys.stderr)
            return 1

    if _check_already_applied(db_path):
        if not force:
            print(
                f"ERROR: migration {MIGRATION_VERSION} already applied to "
                f"{db_path}. Pass --force to wipe and redo.",
                file=sys.stderr,
            )
            return 1
        print(f"--force: wiping existing {db_path}")
        db_path.unlink()
        # WAL/SHM siblings, if any
        for sib in (db_path.with_suffix(db_path.suffix + "-wal"),
                    db_path.with_suffix(db_path.suffix + "-shm")):
            if sib.exists():
                sib.unlink()
    elif db_path.exists() and force:
        print(f"--force: wiping existing {db_path} (schema present, migration not recorded)")
        db_path.unlink()

    inventory_manager.assert_not_locked(xlsx_path)

    print(f"Reading xlsx:    {xlsx_path}")
    rows = inventory_manager.load_inventory(xlsx_path)
    if not rows:
        print("ERROR: xlsx is empty — nothing to migrate.", file=sys.stderr)
        return 1
    print(f"  rows:          {len(rows)}")

    # Build all records first; abort before touching the DB if anything
    # fails. Old→new ID mapping is recorded so we can write the
    # id_format_migration changelog rows.
    print("Building records (validating + backfilling from disk)…")
    records: list[dict[str, Any]] = []
    id_map: list[tuple[str, str]] = []  # (old_id, new_id)
    for row in rows:
        old_id = str(row.get("dataset_id") or "").strip()
        if not old_id:
            raise MigrationError("xlsx row has no dataset_id")
        new_id = _new_dataset_id()
        rec = _build_dataset_record(row, new_id, library_root)
        records.append(rec)
        id_map.append((old_id, new_id))
    print(f"  built:         {len(records)} records (all files readable)")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Creating db:     {db_path}")
    try:
        with closing(_connect(db_path)) as conn:
            _apply_schema(conn)

            now = utc_now_iso()

            placeholders = ", ".join(["?"] * len(DATASET_COLUMNS))
            cols_sql = ", ".join(DATASET_COLUMNS)
            insert_dataset = (
                f"INSERT INTO datasets ({cols_sql}) VALUES ({placeholders})"
            )
            insert_changelog = (
                "INSERT INTO changelog "
                "(id, timestamp, dataset_id, action, field_changed, "
                " old_value, new_value, note, actor) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )

            with conn:  # single transaction
                for rec, (old_id, new_id) in zip(records, id_map):
                    conn.execute(
                        insert_dataset,
                        tuple(rec[c] for c in DATASET_COLUMNS),
                    )
                    # 1) row-level migration audit
                    conn.execute(
                        insert_changelog,
                        (
                            _new_changelog_id(),
                            now,
                            new_id,
                            "migrated_from_xlsx",
                            None,
                            None,
                            None,
                            f"Imported from inventory.xlsx (path: {rec['file_path']})",
                            MIGRATION_ACTOR,
                        ),
                    )
                    # 2) id-format trace: old hex → new ULID
                    conn.execute(
                        insert_changelog,
                        (
                            _new_changelog_id(),
                            now,
                            new_id,
                            "id_format_migration",
                            "dataset_id",
                            old_id,
                            new_id,
                            "Re-keyed from ds_<hex> to ds_<ULID> per schema.sql",
                            MIGRATION_ACTOR,
                        ),
                    )

                # Record the migration itself
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at, description) "
                    "VALUES (?, ?, ?)",
                    (MIGRATION_VERSION, now, MIGRATION_DESCRIPTION),
                )

            # --- post-insert validation ---------------------------------
            print("Validating…")
            problems = _validate(conn, expected_count=len(records))
            if problems:
                raise MigrationError(
                    "post-insert validation failed:\n  - "
                    + "\n  - ".join(problems)
                )
            print(f"  datasets:      {len(records)}")
            print(f"  changelog:     {len(records) * 2}")
            print(f"  schema_migrations: 1 (version {MIGRATION_VERSION})")

    except Exception:
        # Atomic-ish: leave no half-built db.
        if db_path.exists():
            print(f"ERROR: rolling back, removing {db_path}", file=sys.stderr)
            db_path.unlink()
        raise

    # Backup the xlsx only after the db is fully validated.
    backup_path = xlsx_path.with_suffix(xlsx_path.suffix + BACKUP_SUFFIX)
    if xlsx_path != backup_path:
        if backup_path.exists():
            print(
                f"NOTE: backup already exists at {backup_path.name}; not overwriting.",
            )
        else:
            xlsx_path.rename(backup_path)
            print(f"Backed up xlsx:  {xlsx_path.name} → {backup_path.name}")

    # Summary
    print()
    print("─" * 60)
    print(f"Migration {MIGRATION_VERSION} complete.")
    print(f"  datasets migrated:    {len(records)}")
    print(f"  changelog entries:    {len(records) * 2}  "
          f"(2 per dataset: migrated_from_xlsx + id_format_migration)")
    print(f"  database:             {db_path}")
    print(f"  xlsx backup:          {backup_path}")
    print()
    print("ID re-keying (old → new):")
    for old_id, new_id in id_map:
        print(f"  {old_id}  →  {new_id}")
    print("─" * 60)
    return 0


def _validate(conn: sqlite3.Connection, *, expected_count: int) -> list[str]:
    problems: list[str] = []

    cur = conn.execute("SELECT COUNT(*) FROM datasets")
    n = cur.fetchone()[0]
    if n != expected_count:
        problems.append(f"datasets row count {n} != expected {expected_count}")

    cur = conn.execute(
        "SELECT COUNT(*) FROM datasets "
        "GROUP BY dataset_id HAVING COUNT(*) > 1"
    )
    if cur.fetchone() is not None:
        problems.append("duplicate dataset_id detected")

    cur = conn.execute("SELECT COUNT(*) FROM changelog")
    cl_n = cur.fetchone()[0]
    if cl_n != expected_count * 2:
        problems.append(f"changelog row count {cl_n} != expected {expected_count * 2}")

    cur = conn.execute(
        "SELECT cl.id FROM changelog cl "
        "LEFT JOIN datasets d ON d.dataset_id = cl.dataset_id "
        "WHERE d.dataset_id IS NULL"
    )
    orphans = [r[0] for r in cur.fetchall()]
    if orphans:
        problems.append(f"{len(orphans)} changelog rows have no matching dataset (FK orphans)")

    cur = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?", (MIGRATION_VERSION,)
    )
    if cur.fetchone() is None:
        problems.append(f"schema_migrations missing version {MIGRATION_VERSION}")

    # Sniff: STRICT-mode + CHECKs would have already rejected bad rows
    # at INSERT time, but explicitly assert NOT NULL on the always-required
    # snapshot columns.
    cur = conn.execute(
        "SELECT dataset_id FROM datasets WHERE "
        "checksum_sha256 IS NULL OR size_bytes IS NULL OR mtime IS NULL "
        "OR crs IS NULL OR classification IS NULL"
    )
    bad = [r[0] for r in cur.fetchall()]
    if bad:
        problems.append(f"{len(bad)} dataset rows have NULL in required snapshot columns")

    return problems


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="migration-001",
        description=__doc__.splitlines()[0] if __doc__ else None,
    )
    p.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    p.add_argument(
        "--force",
        action="store_true",
        help="Wipe an existing inventory.db before re-running.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return run(
            xlsx_path=args.xlsx,
            db_path=args.db,
            library_root=args.library,
            force=args.force,
        )
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
