"""Ingestion: three-phase promotion from queue/incoming/ into library/.

See DESIGN.md §8 for the full design. Summary:

Phase 1 — ``scan()`` (lenient):
    Walk queue/incoming/. For each recognised single-layer source
    (Shapefile, GPKG, GeoJSON, KML/KMZ, single-band GeoTIFF), capture
    source metadata, move the file (and any sidecars) into
    queue/processing/, append a row to queue/processing/pending.xlsx
    with source columns filled and an auto-proposed target_filename.
    Multi-layer / multi-band sources are rejected here with a clear
    "extract layers first" message; unrecognised extensions are skipped
    silently. **No format / CRS / naming validation at this stage** —
    that runs against the *transformed* file at approval time.

Phase 2 — Steward review (Excel):
    Steward fills required extrinsic metadata (summary, description,
    tags, …), confirms target_filename, supplies classification for
    rasters, and flips ``ready=TRUE`` on rows that are ready to promote.

Phase 3 — ``approve()``:
    For each ready row: run the transformation (vector → GPKG,
    raster → COG, both reprojected to ESRI:102008 with the rules in
    DESIGN.md §11), then run the **strict canonical validators** on the
    transformed file, then compute the intrinsic snapshot, then move
    the canonical file to library/, archive the source bundle to
    queue/archived/<dataset_id>/, append to inventory + changelog.

Ingestion is the ONLY sanctioned way to introduce data into library/.
Manual drops surface as orphans during reconciliation.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, NamedTuple

import fiona
import rasterio
import yaml
from pyproj import Transformer

from . import (
    inventory_manager, pending_sheet, source_formats, taxonomy,
    transformations, utils,
)
from .source_formats import (
    SHAPEFILE_SIDECAR_EXTS,
    SourceMetadata,
    SourceRejected,
    candidate_paths,
    raster_bundle,
    shapefile_bundle,
)
from .validators import validate_all


class ScanResult(NamedTuple):
    pending_path: Path
    accepted: int
    rejected: int


class ApproveResult(NamedTuple):
    pending_path: Path
    promoted: int
    failed: int
    skipped: int  # rows where ready != TRUE
    pending_deleted: bool


# Map source format → canonical (target) extension.
_TARGET_EXT_BY_FORMAT: dict[str, str] = {
    "Shapefile": ".gpkg",
    "GeoPackage": ".gpkg",
    "GeoJSON": ".gpkg",
    "KML": ".gpkg",
    "GeoTIFF": ".tif",
}

# Inventory ``format`` label by target extension. These display names
# show up in pending.xlsx; they get lowercased to the schema's CHECK
# values ('geopackage', 'geotiff') in :func:`_normalize_for_insert`.
_FORMAT_LABEL_BY_TARGET: dict[str, str] = {
    ".gpkg": "GeoPackage",
    ".tif": "Cloud Optimized GeoTIFF",
}

# Pending → schema lowercase mappings. Keep them in sync with the
# CHECK constraints in pipeline/schema.sql.
_FORMAT_TO_SCHEMA: dict[str, str] = {
    "GeoPackage": "geopackage",
    "Cloud Optimized GeoTIFF": "geotiff",
    "GeoTIFF": "geotiff",
    # tolerate a steward who already typed the schema value
    "geopackage": "geopackage",
    "geotiff": "geotiff",
}
_SOURCE_FORMAT_TO_SCHEMA: dict[str, str] = {
    "Shapefile": "shapefile",
    "GeoPackage": "geopackage",
    "GeoJSON": "geojson",
    "KML": "kml",
    "GeoTIFF": "geotiff",
}

# Canonical project CRS (matches schema.sql + lifecycle convention).
_CANONICAL_CRS = "ESRI:102008"


# === Phase 1: scan ====================================================

def scan(incoming_dir: Path, processing_dir: Path, rejected_dir: Path) -> ScanResult:
    """Phase 1: lenient acceptance + source-metadata capture.

    Each accepted source bundle is moved into its own per-dataset
    subdirectory ``queue/processing/<dataset_id>/`` so the transformer
    can write output under any name (including the source's own) without
    clobbering the source. The source bundle stays in that subdirectory
    until approve() promotes it (then the whole subdirectory moves to
    ``queue/archived/<dataset_id>/``).
    """
    incoming_dir.mkdir(parents=True, exist_ok=True)
    processing_dir.mkdir(parents=True, exist_ok=True)
    rejected_dir.mkdir(parents=True, exist_ok=True)

    pending_path = processing_dir / pending_sheet.PENDING_FILENAME
    new_rows: list[dict[str, Any]] = []
    accepted = 0
    rejected = 0

    for entry in candidate_paths(incoming_dir):
        try:
            metadata = source_formats.inspect(entry)
        except SourceRejected as exc:
            _reject_bundle_for(entry, rejected_dir, [("source", str(exc))])
            rejected += 1
            continue

        if metadata.source_format == "Shapefile":
            bundle = shapefile_bundle(entry)
        elif metadata.source_format == "GeoTIFF":
            bundle = raster_bundle(entry)
        else:
            bundle = [entry]

        dataset_id = utils.new_dataset_id()
        dataset_staging = processing_dir / dataset_id
        if dataset_staging.exists():
            # Astronomically unlikely (uuid4), but be defensive.
            _reject_bundle_for(
                entry, rejected_dir,
                [("staging", f"dataset_id collision in processing/: {dataset_id}")],
            )
            rejected += 1
            continue
        dataset_staging.mkdir(parents=True)

        for member in bundle:
            member.rename(dataset_staging / member.name)

        primary_in_staging = dataset_staging / entry.name
        new_rows.append(_build_row(primary_in_staging, metadata, dataset_id=dataset_id))
        accepted += 1

    if new_rows:
        pending_sheet.append_pending(pending_path, new_rows)

    return ScanResult(pending_path=pending_path, accepted=accepted, rejected=rejected)


# === Phase 3: approve =================================================

def approve(
    processing_dir: Path,
    library_root: Path,
    db_path: Path,
    *,
    actor: str,
    archived_dir: Path | None = None,
) -> ApproveResult:
    """Phase 3: transform → validate → snapshot → promote → archive source.

    ``db_path`` points at ``inventory/inventory.db`` (the SQLite
    catalogue). The inventory and changelog are both inside it.
    """
    pending_path = processing_dir / pending_sheet.PENDING_FILENAME
    if archived_dir is None:
        archived_dir = processing_dir.parent / "archived"
    archived_dir.mkdir(parents=True, exist_ok=True)

    if not pending_path.exists():
        return ApproveResult(pending_path, 0, 0, 0, False)

    # Fail fast if Excel has the pending sheet open — otherwise file
    # moves could happen before the catalogue write fails, producing
    # orphans. The catalogue is SQLite and not subject to Excel locks,
    # so no equivalent guard is needed there.
    inventory_manager.assert_not_locked(pending_path)

    rows = pending_sheet.load_pending(pending_path)
    promoted = failed = skipped = 0
    next_pending: list[dict[str, Any]] = []

    for row in rows:
        if not _is_ready(row):
            next_pending.append(row)
            skipped += 1
            continue

        # Pre-validate steward declarations before doing any expensive transformation.
        decl_errors = _validate_declarations(row, processing_dir)
        if decl_errors:
            row[pending_sheet.READY_COLUMN] = False
            row[pending_sheet.ERROR_COLUMN] = "; ".join(decl_errors)
            next_pending.append(row)
            failed += 1
            continue

        try:
            target_path = _run_transformation(row, processing_dir)
        except transformations.TransformError as exc:
            row[pending_sheet.READY_COLUMN] = False
            row[pending_sheet.ERROR_COLUMN] = f"transform: {exc}"
            next_pending.append(row)
            failed += 1
            continue

        # Strict canonical validation against the transformed file.
        canonical_failures = validate_all(target_path)
        if canonical_failures:
            target_path.unlink(missing_ok=True)
            row[pending_sheet.READY_COLUMN] = False
            row[pending_sheet.ERROR_COLUMN] = "post-transform: " + "; ".join(
                f"{check}: {reason}" for check, reason in canonical_failures
            )
            next_pending.append(row)
            failed += 1
            continue

        # Compute intrinsic snapshot on the transformed file and merge into row.
        try:
            _populate_snapshot(row, target_path)
        except Exception as exc:  # pragma: no cover — defensive
            target_path.unlink(missing_ok=True)
            row[pending_sheet.READY_COLUMN] = False
            row[pending_sheet.ERROR_COLUMN] = f"snapshot failed: {exc}"
            next_pending.append(row)
            failed += 1
            continue

        try:
            _promote(row, target_path, library_root, db_path, actor=actor)
            _archive_source(row, processing_dir, archived_dir)
            promoted += 1
        except Exception as exc:  # pragma: no cover — defensive
            row[pending_sheet.READY_COLUMN] = False
            row[pending_sheet.ERROR_COLUMN] = f"promotion failed: {exc}"
            next_pending.append(row)
            failed += 1

    pending_sheet.save_pending(pending_path, next_pending)
    deleted = pending_sheet.delete_if_empty(pending_path)

    return ApproveResult(
        pending_path=pending_path,
        promoted=promoted,
        failed=failed,
        skipped=skipped,
        pending_deleted=deleted,
    )


# === Internals =========================================================

def _reject_bundle_for(
    primary: Path,
    rejected_dir: Path,
    failures: list[tuple[str, str]],
) -> None:
    """Move the primary file (and any sidecars) to rejected/."""
    ext = primary.suffix.lower()
    if ext == ".shp":
        bundle = shapefile_bundle(primary)
    elif ext in (".tif", ".tiff"):
        bundle = raster_bundle(primary)
    else:
        bundle = [primary]

    dest_dir = rejected_dir
    primary_dest = dest_dir / primary.name
    if primary_dest.exists():
        # Disambiguate so we don't clobber a prior rejection.
        stem, suffix = primary.stem, primary.suffix
        for n in range(1, 1000):
            alt = dest_dir / f"{stem}__{n}{suffix}"
            if not alt.exists():
                primary_dest = alt
                # Adjust bundle destinations to use the disambiguated stem.
                rename_to_stem = alt.stem
                break
        else:
            raise RuntimeError("could not allocate a non-clobbering rejection name")
    else:
        rename_to_stem = primary.stem

    for member in bundle:
        # Preserve the sidecar's compound suffix relative to the original stem.
        rel_suffix = member.name[len(primary.stem):]
        member.rename(dest_dir / (rename_to_stem + rel_suffix))

    reason_path = dest_dir / (rename_to_stem + primary.suffix + ".rejected.yaml")
    payload = {
        "original_name": primary.name,
        "rejected_at": utils.utc_now_iso(),
        "failures": [{"check": check, "reason": reason} for check, reason in failures],
    }
    reason_path.write_text(yaml.safe_dump(payload, sort_keys=False))


def _build_row(
    primary_in_processing: Path,
    meta: SourceMetadata,
    *,
    dataset_id: str,
) -> dict[str, Any]:
    """Compose a fresh pending row at scan time. No intrinsic snapshot yet."""
    target_ext = _TARGET_EXT_BY_FORMAT[meta.source_format]
    target_filename = utils.slugify_title(primary_in_processing.stem) + target_ext
    today = utils.utc_now_iso()

    # Best-guess category from filename — steward can override.
    guess_text = primary_in_processing.stem
    guessed_category = taxonomy.guess_category(guess_text)
    guessed_subcategory = taxonomy.guess_subcategory(guessed_category, guess_text)

    return {
        # control
        pending_sheet.READY_COLUMN: False,
        # identity & location
        "dataset_id": dataset_id,
        "category": guessed_category,
        "subcategory": guessed_subcategory,
        "file_path": None,                    # set at approve
        "format": _FORMAT_LABEL_BY_TARGET[target_ext],
        # source provenance (locked at scan)
        "source_format": meta.source_format,
        "source_filename": meta.source_filename,
        "source_crs": meta.source_crs,
        "source_layer": meta.source_layer,
        # transform inputs
        pending_sheet.TARGET_FILENAME_COLUMN: target_filename,
        # intrinsic snapshot (computed at approve)
        "crs": None,
        "checksum_sha256": None,
        "size_bytes": None,
        "mtime": None,
        "geographic_extent_bbox": None,
        # classification — auto-filled for vector; steward declares for raster
        "classification": (
            taxonomy.VECTOR_CLASSIFICATION if meta.is_vector else None
        ),
        # history & governance
        "status": "active",
        "date_added": today,
        "date_modified": today,
        "data_steward": None,
        # extrinsic
        "summary": None,
        "description": None,
        "tags": None,
        "terms_of_use": None,
        "acknowledgements": None,
        # AGOL linkage & freeform
        "agol_item_id": None,
        "notes": None,
        # error sink
        pending_sheet.ERROR_COLUMN: None,
    }


# Required-non-empty fields a steward must fill before approval.
_REQUIRED_NON_EMPTY = (
    "title", "category", "format",
    pending_sheet.TARGET_FILENAME_COLUMN,
    "status", "data_steward",
    "summary", "description", "tags", "terms_of_use", "acknowledgements",
)


def _is_ready(row: dict[str, Any]) -> bool:
    v = row.get(pending_sheet.READY_COLUMN)
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().upper() in ("TRUE", "T", "YES", "Y", "1")
    if isinstance(v, (int, float)):
        return bool(v)
    return False


def _validate_declarations(row: dict[str, Any], processing_dir: Path) -> list[str]:
    """Validate steward-supplied fields *before* expensive transformation."""
    errors: list[str] = []

    # dataset_id sanity (needed before path lookup). The schema's only
    # constraint is the ``ds_`` prefix; we additionally require enough
    # suffix length to rule out a one-character typo. Both the legacy
    # 12-hex (15 char total) and the post-001 26-char ULID (29 char
    # total) are accepted.
    did = row.get("dataset_id")
    if not (isinstance(did, str) and did.startswith("ds_") and len(did) >= 15):
        return ["dataset_id is malformed (locked column was edited or row is corrupt)"]

    src_name = row.get("source_filename")
    if not src_name:
        return ["source_filename is missing from the row"]
    src_path = processing_dir / did / str(src_name)
    if not src_path.exists():
        return [
            f"source file '{src_name}' not found in queue/processing/{did}/ "
            f"(steward may have edited source_filename, or the bundle was moved)"
        ]

    # Required-non-empty checks
    for field in _REQUIRED_NON_EMPTY:
        v = row.get(field)
        if v is None or (isinstance(v, str) and not v.strip()):
            errors.append(f"required field '{field}' is empty")

    # Category enum
    cat = row.get("category")
    if cat and cat not in taxonomy.CATEGORIES:
        errors.append(
            f"category '{cat}' is not one of the {len(taxonomy.CATEGORIES)} canonical categories"
        )

    # Subcategory rules
    sub = row.get("subcategory")
    if cat in taxonomy.CATEGORIES:
        if not taxonomy.is_valid_subcategory(cat, sub if sub else None):
            allowed = taxonomy.SUBCATEGORIES.get(cat)
            if allowed:
                errors.append(
                    f"subcategory '{sub}' is not valid for category '{cat}'; "
                    f"allowed: {', '.join(allowed)}"
                )
            else:
                errors.append(f"subcategory must be empty for category '{cat}'")

    # Status enum
    status = row.get("status")
    if status and status not in taxonomy.INGEST_STATUSES:
        errors.append(
            f"status '{status}' is not allowed at ingest; use 'active' or 'deprecated'"
        )

    # Classification: must match the target type.
    #   .gpkg → "vector"
    #   .tif  → "continuous" or "categorical"
    target_filename = row.get(pending_sheet.TARGET_FILENAME_COLUMN) or ""
    target_ext = Path(str(target_filename)).suffix.lower()
    classification = row.get("classification")
    if target_ext == ".tif":
        if classification not in taxonomy.RASTER_CLASSIFICATIONS:
            errors.append(
                f"classification must be one of {taxonomy.RASTER_CLASSIFICATIONS} "
                f"for raster datasets (got {classification!r})"
            )
    elif target_ext == ".gpkg":
        if classification != taxonomy.VECTOR_CLASSIFICATION:
            errors.append(
                f"classification must be '{taxonomy.VECTOR_CLASSIFICATION}' "
                f"for vector datasets (got {classification!r})"
            )

    # target_filename must satisfy the naming convention (lowercase + underscore + .gpkg/.tif)
    from .validators.naming import validate_naming
    if target_filename:
        ok, reason = validate_naming(Path(str(target_filename)))
        if not ok:
            errors.append(f"target_filename: {reason}")

    return errors


def _run_transformation(row: dict[str, Any], processing_dir: Path) -> Path:
    """Run the source→canonical transformation. Returns the transformed file path."""
    dataset_id = str(row["dataset_id"])
    src_name = str(row["source_filename"])
    src_path = processing_dir / dataset_id / src_name
    target_filename = str(row[pending_sheet.TARGET_FILENAME_COLUMN])
    # Transformer writes to a per-dataset *output* subdirectory so a
    # canonical-named target never collides with anything else.
    target_dir = processing_dir / dataset_id / "_canonical"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / target_filename
    target_ext = Path(target_filename).suffix.lower()

    if target_ext == ".gpkg":
        transformations.vector_to_canonical(src_path, target_path)
    elif target_ext == ".tif":
        classification = str(row["classification"])
        transformations.raster_to_canonical(src_path, target_path, classification)
    else:
        raise transformations.TransformError(
            f"unsupported target extension '{target_ext}' (expected .gpkg or .tif)"
        )
    return target_path


def _populate_snapshot(row: dict[str, Any], target_path: Path) -> None:
    """Compute intrinsic-snapshot fields from the transformed file and merge into row.

    Populates the always-present columns (checksum, size, mtime, crs,
    bbox) plus the type-specific columns the SQLite schema added in
    migration 001 (``footprint_wkt``, ``feature_count`` for vectors,
    ``raster_*`` and ``pixel_size_*`` for rasters).
    """
    size_bytes, mtime = utils.stat_signature(target_path)
    checksum = utils.sha256_file(target_path)
    if utils.is_vector(target_path):
        crs = utils.read_vector_crs(target_path)
        bbox = utils.read_vector_bbox(target_path)
        with fiona.open(target_path) as src:
            row["feature_count"] = len(src)
        row["raster_width"] = None
        row["raster_height"] = None
        row["pixel_size_x"] = None
        row["pixel_size_y"] = None
    else:
        crs = utils.read_raster_crs(target_path)
        bbox = utils.read_raster_bbox(target_path)
        with rasterio.open(target_path) as ds:
            row["raster_width"] = int(ds.width)
            row["raster_height"] = int(ds.height)
            row["pixel_size_x"] = float(ds.transform.a)
            row["pixel_size_y"] = float(-ds.transform.e)
        row["feature_count"] = None

    row["crs"] = utils.crs_to_authority_string(crs) if crs else None
    row["checksum_sha256"] = checksum
    row["size_bytes"] = size_bytes
    row["mtime"] = mtime
    row["geographic_extent_bbox"] = utils.bbox_to_string(bbox) if bbox else None
    row["footprint_wkt"] = _bbox_to_footprint_wkt(
        utils.bbox_to_string(bbox) if bbox else None
    )
    row["date_modified"] = utils.utc_now_iso()


def _bbox_to_footprint_wkt(bbox_str: str | None) -> str | None:
    """Reproject canonical-CRS bbox to EPSG:4326 as a 4-corner POLYGON WKT.

    Mirrors the helper in ``migrations/001_xlsx_to_sqlite.py``; kept
    inline here to avoid the digit-prefixed migration module not being
    importable.
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
    tf = Transformer.from_crs(_CANONICAL_CRS, "EPSG:4326", always_xy=True)
    lon_min, lat_min, lon_max, lat_max = tf.transform_bounds(
        minx, miny, maxx, maxy, densify_pts=21
    )
    return (
        "POLYGON(("
        f"{lon_min:.6f} {lat_min:.6f}, "
        f"{lon_max:.6f} {lat_min:.6f}, "
        f"{lon_max:.6f} {lat_max:.6f}, "
        f"{lon_min:.6f} {lat_max:.6f}, "
        f"{lon_min:.6f} {lat_min:.6f}"
        "))"
    )


def _normalize_for_insert(row: dict[str, Any]) -> dict[str, Any]:
    """Project a pending row onto the canonical ``datasets`` schema columns.

    Specifically:

    * Drop pending-only fields (``ready``, ``target_filename``,
      ``_validation_error``).
    * Rename ``notes`` → ``internal_notes``.
    * Lowercase ``format`` and ``source_format`` to the schema's CHECK
      values.
    * Fill in schema fields the pending sheet doesn't carry:
      ``dataset_type='spatial'``, ``sync_status='unpublished'``.
    """
    out: dict[str, Any] = dict(row)

    for k in (pending_sheet.READY_COLUMN,
              pending_sheet.TARGET_FILENAME_COLUMN,
              pending_sheet.ERROR_COLUMN):
        out.pop(k, None)

    if "notes" in out and "internal_notes" not in out:
        out["internal_notes"] = out.pop("notes")
    else:
        out.pop("notes", None)

    fmt = out.get("format")
    if isinstance(fmt, str):
        if fmt not in _FORMAT_TO_SCHEMA:
            raise ValueError(f"unknown format value {fmt!r}")
        out["format"] = _FORMAT_TO_SCHEMA[fmt]

    src_fmt = out.get("source_format")
    if isinstance(src_fmt, str) and src_fmt:
        if src_fmt not in _SOURCE_FORMAT_TO_SCHEMA:
            raise ValueError(f"unknown source_format value {src_fmt!r}")
        out["source_format"] = _SOURCE_FORMAT_TO_SCHEMA[src_fmt]

    out.setdefault("dataset_type", "spatial")
    out.setdefault("sync_status", "unpublished")

    return out


def _normalize_tags(value: Any) -> Any:
    """Strip whitespace around each ``;``-separated tag and drop empties."""
    if not isinstance(value, str):
        return value
    parts = [p.strip() for p in value.split(";")]
    return ";".join(p for p in parts if p)


def _promote(
    row: dict[str, Any],
    target_path: Path,
    library_root: Path,
    db_path: Path,
    *,
    actor: str,
) -> Path:
    """Move transformed file into library/, INSERT into datasets + changelog.

    The inventory's ``category`` (and ``subcategory``) values are display
    names — full names from the typology — but on-disk folder names are
    the underscore abbreviations. We map display→folder for the
    filesystem path and keep the display value in the inventory row.

    The pending row's column shape doesn't match ``datasets`` 1:1
    (pending has ``ready``, ``target_filename``, ``_validation_error``,
    legacy ``notes``); :func:`_normalize_for_insert` projects it onto
    the canonical schema columns before insert.
    """
    category_display = str(row["category"])
    subcategory_display = row.get("subcategory")
    target_filename = str(row[pending_sheet.TARGET_FILENAME_COLUMN])

    category_folder = taxonomy.CATEGORY_FOLDERS[category_display]
    if subcategory_display:
        sub_folder = taxonomy.SUBCATEGORY_FOLDERS[category_display][subcategory_display]
        rel_target = Path(category_folder) / sub_folder / target_filename
    else:
        rel_target = Path(category_folder) / target_filename

    library_full = library_root / rel_target
    library_full.parent.mkdir(parents=True, exist_ok=True)

    if library_full.exists():
        raise FileExistsError(f"target already exists in library: {rel_target}")

    target_path.rename(library_full)

    finalized = dict(row)
    finalized["file_path"] = str(rel_target)
    finalized["tags"] = _normalize_tags(finalized.get("tags"))
    canonical_row = _normalize_for_insert(finalized)

    inventory_manager.insert_dataset(db_path, canonical_row)
    inventory_manager.append_changelog(
        db_path,
        timestamp=utils.utc_now_iso(),
        action="add",
        dataset_id=str(row["dataset_id"]),
        actor=actor,
        path=str(rel_target),
        detail=(
            f"Ingested '{row.get('title', '')}'; "
            f"source: {row.get('source_format')} '{row.get('source_filename')}'."
        ),
    )
    return library_full


def _archive_source(
    row: dict[str, Any], processing_dir: Path, archived_dir: Path,
) -> None:
    """Move the per-dataset processing/<dataset_id>/ subtree to archived/<dataset_id>/.

    Drops the transient _canonical/ output subdirectory in the process —
    only the source bundle is preserved.
    """
    dataset_id = str(row["dataset_id"])
    staging = processing_dir / dataset_id
    if not staging.exists():
        return  # Defensive — nothing to archive.

    canonical_dir = staging / "_canonical"
    if canonical_dir.exists():
        shutil.rmtree(canonical_dir)

    dest = archived_dir / dataset_id
    if dest.exists():
        # Should not happen; dataset_id is unique. Defensive cleanup.
        shutil.rmtree(dest)
    staging.rename(dest)
