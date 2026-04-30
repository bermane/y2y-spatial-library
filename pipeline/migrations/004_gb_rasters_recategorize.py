"""Migration 004 — re-stage 6 GB habitat rasters for re-ingest as categorical.

Why
---
The 6 Grizzly Bear habitat rasters under
``library/spatial/Species/Grizzly_Bear/`` were ingested with
``classification='continuous'`` but are actually categorical
(integer-coded habitat class IDs 1–10). Bilinear resampling during
the initial ingest smeared every class boundary into millions of
impossible mid-values (the on-disk Float32 raster carries 15.4M
unique values between 1.0 and 10.0). The current pixel data cannot
be recovered without re-ingesting from source.

This migration sets up that re-ingest by:

1. Saving the steward-authored extrinsic metadata (title, summary,
   description, tags, terms_of_use, acknowledgements, data_steward,
   internal_notes, agol_item_id) for each of the 6 rows.
2. Reading the original UTM 11N source TIFFs from
   ``queue/archived/<dataset_id>/`` and writing them into
   ``queue/incoming/`` after a **lossless Float32 → UInt8 dtype cast**
   (the source values are integer-coded class IDs 1–10 stored as
   Float32; the canonical schema requires UInt8 for categorical per
   DESIGN.md §9). The cast aborts if any pixel is non-integer or
   outside the safe ``[1, 254]`` range.
3. Deleting the 6 corrupted library files.
4. **Hard-deleting** the 6 ``datasets`` rows and their ``changelog``
   history.
5. Running ``ingest.scan`` so a fresh ``pending.xlsx`` is generated
   with new ULID dataset_ids.
6. Applying the saved metadata onto those new pending rows, plus
   ``classification='categorical'`` and ``ready=True``.
7. Recording itself in ``schema_migrations``.

Step 4 is a deliberate exception to the "tombstone, never delete"
policy in DESIGN.md §5/§10. The ``changelog.dataset_id`` foreign key
is normally ``ON DELETE RESTRICT`` to keep audit history intact;
this migration temporarily disables FKs (``PRAGMA foreign_keys =
OFF``) so the rows and their history can be removed cleanly. The
trade is documented and the steward consented because the project is
still pre-production. Future re-ingests should prefer
``y2y tombstone`` instead.

After this migration completes, run ``y2y ingest --approve`` to
promote the pending rows into 6 fresh active dataset rows
(re-ingested with nearest-neighbour resampling and clean 30 m pixel
size).

Behaviour
---------
* **Idempotent.** Refuses to run if version ``'004'`` is already
  recorded in ``schema_migrations`` (no ``--force`` — this migration
  is too situation-specific to re-run).
* **Pre-flight.** Confirms migrations 001–003 applied, all 6 GB rows
  are still ``status='active'``, all 6 source bundles exist in
  ``queue/archived/<dataset_id>/``, and ``queue/processing/pending.xlsx``
  is absent (no in-flight steward work to clobber).
* **Audit.** Records itself in ``schema_migrations``. Does **not**
  write per-dataset changelog rows for the deletion — there's no
  surviving dataset row to FK to. The schema_migrations row is the
  only audit record for this cleanup.

Usage
-----
::

    python pipeline/migrations/004_gb_rasters_recategorize.py
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import numpy as np
import rasterio

from pipeline import ingest, pending_sheet
from pipeline import db as _db
from pipeline.utils import utc_now_iso

MIGRATION_VERSION = "004"
MIGRATION_DESCRIPTION = (
    "Hard-delete 6 GB habitat raster rows + files for re-ingest as categorical "
    "(continuous→categorical correction; FK constraint bypassed)"
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "inventory" / "inventory.db"
DEFAULT_LIBRARY = PROJECT_ROOT / "library" / "spatial"
DEFAULT_ARCHIVED = PROJECT_ROOT / "queue" / "archived"
DEFAULT_INCOMING = PROJECT_ROOT / "queue" / "incoming"
DEFAULT_PROCESSING = PROJECT_ROOT / "queue" / "processing"
DEFAULT_REJECTED = PROJECT_ROOT / "queue" / "rejected"

# The 6 GB habitat datasets, identified by file_path. Source-of-truth list
# for what this migration touches; aborts if a row is missing or extra.
_GB_FILE_PATHS = (
    "Species/Grizzly_Bear/gb_habitat_female_fall.tif",
    "Species/Grizzly_Bear/gb_habitat_female_spring.tif",
    "Species/Grizzly_Bear/gb_habitat_female_summer.tif",
    "Species/Grizzly_Bear/gb_habitat_male_fall.tif",
    "Species/Grizzly_Bear/gb_habitat_male_spring.tif",
    "Species/Grizzly_Bear/gb_habitat_male_summer.tif",
)

# Steward-authored extrinsic fields to preserve and re-apply.
_PRESERVED_FIELDS = (
    "title", "summary", "description", "tags",
    "terms_of_use", "acknowledgements", "data_steward",
    "internal_notes", "agol_item_id",
    "category", "subcategory",
)


class MigrationError(RuntimeError):
    pass


class _ConversionError(RuntimeError):
    pass


def _convert_float32_class_codes_to_uint8(src: Path, dst: Path) -> None:
    """Lossless Float32 → UInt8 conversion for integer-coded class rasters.

    Aborts (raises ``_ConversionError``) if the source isn't safely
    castable: any non-integer-valued pixel, any valid value outside
    ``[1, 254]``, or any unexpected dtype.

    Nodata mapping:
        Float32 sentinel (anything < -1e30) → UInt8 255.
        Source must declare a ``nodata`` value; otherwise we don't
        know which Float32 pixels to mask.

    Output preserves source CRS, transform, dimensions, and tiling.
    Other source-side metadata (nodata, dtype, predictor) is rewritten
    to match the UInt8 categorical convention.
    """
    with rasterio.open(src) as ds:
        if ds.dtypes[0] != "float32":
            raise _ConversionError(
                f"unexpected dtype {ds.dtypes[0]!r}; this conversion only "
                f"handles float32 → uint8."
            )
        if ds.nodata is None:
            raise _ConversionError(
                "source has no declared nodata; cannot safely identify "
                "background pixels."
            )

        arr = ds.read(1)
        # Float32 sentinels are typically very negative (~ -3.4e38).
        nodata_mask = arr <= -1e30
        valid = arr[~nodata_mask]

        if valid.size == 0:
            raise _ConversionError("source has zero valid (non-nodata) pixels.")
        # Tolerance because Float32 stores small fractional drift even
        # for integer values.
        if not np.allclose(valid, np.round(valid), atol=1e-3):
            raise _ConversionError(
                "source has non-integer-valued pixels; this conversion is "
                "only safe for integer-coded class rasters."
            )
        valid_int = np.round(valid).astype(np.int32)
        if valid_int.min() < 1 or valid_int.max() > 254:
            raise _ConversionError(
                f"valid value range [{valid_int.min()}, {valid_int.max()}] "
                f"is outside the safe UInt8 range [1, 254] (255 reserved "
                f"for nodata)."
            )

        out = np.full(arr.shape, 255, dtype=np.uint8)
        out[~nodata_mask] = valid_int.astype(np.uint8)

        profile = {
            "driver": "GTiff",
            "dtype": "uint8",
            "count": 1,
            "width": ds.width,
            "height": ds.height,
            "crs": ds.crs,
            "transform": ds.transform,
            "tiled": True,
            "blockxsize": 512,
            "blockysize": 512,
            "compress": "zstd",
            "predictor": 2,         # uint8 categorical
            "zstd_level": 9,
            "nodata": 255,
            "BIGTIFF": "IF_NEEDED",
        }

        dst.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(dst, "w", **profile) as out_ds:
            out_ds.write(out, 1)


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
    missing = [v for v in ("001", "002", "003") if v not in have]
    return missing


def run(
    *,
    db_path: Path,
    library_root: Path,
    archived_dir: Path,
    incoming_dir: Path,
    processing_dir: Path,
    rejected_dir: Path,
) -> int:
    if not db_path.exists():
        print(f"ERROR: catalogue not found at {db_path}", file=sys.stderr)
        return 1

    # --- pre-flight ----------------------------------------------------
    with closing(_db.get_connection(db_path)) as conn:
        if _migration_already_applied(conn):
            print(
                f"ERROR: migration {MIGRATION_VERSION} already applied. "
                f"This migration is one-shot and intentionally has no --force.",
                file=sys.stderr,
            )
            return 1

        missing = _check_prereq_migrations(conn)
        if missing:
            print(
                f"ERROR: prerequisite migrations not applied: {missing}. "
                f"Run them first.",
                file=sys.stderr,
            )
            return 1

        # Confirm all 6 GB rows are present, active, with files on disk
        # and source bundles in the archive.
        rows: list[dict[str, Any]] = []
        for fp in _GB_FILE_PATHS:
            cur = conn.execute(
                "SELECT * FROM datasets WHERE file_path = ? AND status = 'active'",
                (fp,),
            )
            r = cur.fetchone()
            if r is None:
                print(
                    f"ERROR: expected active dataset with file_path = {fp!r} "
                    f"not found.",
                    file=sys.stderr,
                )
                return 1
            rows.append(dict(r))

    # File system pre-flight (outside the connection scope so the catalogue
    # is closed during the slow filesystem checks).
    pending_path = processing_dir / pending_sheet.PENDING_FILENAME
    if pending_path.exists():
        existing = pending_sheet.load_pending(pending_path)
        if existing:
            print(
                f"ERROR: {pending_path} already has {len(existing)} row(s). "
                f"Approve or clear them before running this migration.",
                file=sys.stderr,
            )
            return 1

    for r in rows:
        lib_file = library_root / r["file_path"]
        if not lib_file.exists():
            print(
                f"ERROR: library file missing for {r['dataset_id']}: {lib_file}",
                file=sys.stderr,
            )
            return 1
        src_bundle = archived_dir / r["dataset_id"] / r["source_filename"]
        if not src_bundle.exists():
            print(
                f"ERROR: archived source missing for {r['dataset_id']}: {src_bundle}",
                file=sys.stderr,
            )
            return 1

    print(f"Pre-flight OK. Targets: {len(rows)} datasets.")
    print()

    # --- save metadata -------------------------------------------------
    # Keyed by source_filename so we can match against fresh pending
    # rows after scan() allocates new dataset_ids.
    saved: dict[str, dict[str, Any]] = {}
    for r in rows:
        meta = {f: r.get(f) for f in _PRESERVED_FIELDS}
        # Critical correction: classification was 'continuous'; make it
        # 'categorical' on the new pending row.
        meta["classification"] = "categorical"
        saved[r["source_filename"]] = meta
        print(f"  saved metadata for {r['source_filename']}  (was {r['dataset_id']})")
    print()

    # --- stage sources to incoming + dtype-convert Float32→UInt8 -----
    # The archived sources are Float32 with integer values 1–10; the
    # canonical schema requires UInt8 for categorical (DESIGN.md §9).
    # Coerce here (lossless — values are integer-valued floats) instead
    # of bending the pipeline's no-coerce policy (DESIGN.md §11).
    incoming_dir.mkdir(parents=True, exist_ok=True)
    for r in rows:
        src = archived_dir / r["dataset_id"] / r["source_filename"]
        dst = incoming_dir / r["source_filename"]
        if dst.exists():
            print(
                f"ERROR: incoming target already exists: {dst}. "
                f"Move or remove before re-running.",
                file=sys.stderr,
            )
            return 1

        try:
            _convert_float32_class_codes_to_uint8(src, dst)
        except _ConversionError as exc:
            print(f"ERROR converting {src.name}: {exc}", file=sys.stderr)
            return 2
        print(f"  staged + cast UInt8: {r['source_filename']}  ({dst.stat().st_size:,} bytes)")
    print()

    # --- delete library files -----------------------------------------
    for r in rows:
        lib_file = library_root / r["file_path"]
        lib_file.unlink()
        print(f"  deleted library file: {r['file_path']}")
    print()

    # --- DELETE rows + changelog (FK temporarily off) -----------------
    # Open a fresh connection without the project's get_connection
    # helper so we can flip foreign_keys off explicitly. The catalogue's
    # next opener (CLI commands) will get fresh PRAGMA-on behaviour.
    delete_ids = [r["dataset_id"] for r in rows]
    placeholders = ",".join(["?"] * len(delete_ids))

    print("Disabling FK enforcement; deleting rows + changelog history…")
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        with conn:
            cur = conn.execute(
                f"DELETE FROM changelog WHERE dataset_id IN ({placeholders})",
                delete_ids,
            )
            cl_deleted = cur.rowcount
            cur = conn.execute(
                f"DELETE FROM datasets WHERE dataset_id IN ({placeholders})",
                delete_ids,
            )
            ds_deleted = cur.rowcount
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at, description) "
                "VALUES (?, ?, ?)",
                (MIGRATION_VERSION, utc_now_iso(), MIGRATION_DESCRIPTION),
            )
        # Verify integrity is still clean (no orphaned changelog rows
        # left behind that point at other deletions we didn't intend).
        # PRAGMA foreign_key_check returns rows for any current
        # violations.
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            print(
                f"ERROR: foreign_key_check reports {len(violations)} "
                f"violation(s) after delete. Aborting before re-enabling FKs.",
                file=sys.stderr,
            )
            return 2
        conn.execute("PRAGMA foreign_keys = ON")

    print(f"  deleted {ds_deleted} dataset rows, {cl_deleted} changelog rows")
    print()

    # --- run scan() to generate fresh pending.xlsx --------------------
    rejected_dir.mkdir(parents=True, exist_ok=True)
    print(f"Running scan on {incoming_dir}…")
    scan_result = ingest.scan(incoming_dir, processing_dir, rejected_dir)
    print(
        f"  scan accepted: {scan_result.accepted}, "
        f"rejected: {scan_result.rejected}"
    )
    if scan_result.accepted != len(rows):
        print(
            f"ERROR: scan accepted {scan_result.accepted} but expected "
            f"{len(rows)}. Investigate before continuing.",
            file=sys.stderr,
        )
        return 2
    print()

    # --- apply saved metadata onto fresh pending rows -----------------
    pending_rows = pending_sheet.load_pending(pending_path)
    print(f"Applying saved metadata to {len(pending_rows)} pending rows…")
    matched = 0
    for prow in pending_rows:
        src_name = prow.get("source_filename")
        if src_name not in saved:
            continue
        meta = saved[src_name]
        for k, v in meta.items():
            prow[k] = v
        prow[pending_sheet.READY_COLUMN] = True
        matched += 1
        print(f"  applied: {src_name}  →  classification=categorical, ready=True")

    if matched != len(rows):
        print(
            f"ERROR: matched only {matched}/{len(rows)} pending rows. "
            f"Aborting before saving pending.xlsx.",
            file=sys.stderr,
        )
        return 2

    pending_sheet.save_pending(pending_path, pending_rows)
    print(f"  pending sheet saved: {pending_path}")
    print()

    # --- summary -------------------------------------------------------
    print("─" * 60)
    print(f"Migration {MIGRATION_VERSION} complete.")
    print(f"  rows deleted:        {ds_deleted}")
    print(f"  changelog deleted:   {cl_deleted}")
    print(f"  library files removed: {len(rows)}")
    print(f"  sources staged:      {incoming_dir}")
    print(f"  pending populated:   {pending_path}")
    print()
    print("Next step:")
    print("  y2y ingest --approve --actor <name>")
    print("This will transform the staged sources with nearest-neighbour")
    print("resampling and clean 30 m pixel size, validate, and promote them")
    print("into the catalogue as 6 fresh active rows.")
    print("─" * 60)
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="migration-004",
        description=(__doc__ or "").splitlines()[0],
    )
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    p.add_argument("--archived", type=Path, default=DEFAULT_ARCHIVED)
    p.add_argument("--incoming", type=Path, default=DEFAULT_INCOMING)
    p.add_argument("--processing", type=Path, default=DEFAULT_PROCESSING)
    p.add_argument("--rejected", type=Path, default=DEFAULT_REJECTED)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return run(
            db_path=args.db,
            library_root=args.library,
            archived_dir=args.archived,
            incoming_dir=args.incoming,
            processing_dir=args.processing,
            rejected_dir=args.rejected,
        )
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
