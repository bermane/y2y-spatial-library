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
    agol_vtpk, inventory_manager, pending_sheet, source_formats, taxonomy,
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
    # VTPK files (manually built by the steward in ArcGIS Pro and
    # dropped in queue/incoming/) follow a separate path: they don't
    # create new catalogue rows, they attach to an existing
    # vector-tile-layer row by file stem. Each .vtpk yields one
    # IngestVtpkResult so the scan summary can distinguish
    # successful-moves from leftovers in the queue (unmatched /
    # ambiguous / invalid). See pipeline/agol_vtpk.py.
    vtpk_results: tuple["agol_vtpk.IngestVtpkResult", ...] = ()


class VtpkReminder(NamedTuple):
    """A newly-approved VTL row that doesn't yet have a VTPK.

    Surfaced by :func:`approve` so the CLI can print an actionable
    reminder ("build the VTPK in Pro, drop in queue/incoming/"). The
    row is still successfully approved and lands in the catalogue —
    this reminder is informational, not blocking. The pipeline's
    other guards (reconcile's missing-VTPK issue + push's pre-flight
    error) ensure a forgotten VTPK can't be silently shipped to AGOL.
    """

    dataset_id: str
    gpkg_relative_path: str  # the row's file_path
    expected_vtpk_path: Path  # canonical destination


class ApproveResult(NamedTuple):
    pending_path: Path
    promoted: int
    failed: int
    skipped: int  # rows where ready != TRUE
    pending_deleted: bool
    # Rev 3: rows promoted with agol_format='vector-tile-layer' that
    # don't have a corresponding library/vtpk/<stem>.vtpk yet.
    # Default is empty so existing callers/tests don't break.
    vtpk_reminders: tuple[VtpkReminder, ...] = ()


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

def scan(
    incoming_dir: Path,
    processing_dir: Path,
    rejected_dir: Path,
    *,
    library_root: Path | None = None,
    db_path: Path | None = None,
    actor: str = "scan",
) -> ScanResult:
    """Phase 1: lenient acceptance + source-metadata capture.

    Each accepted source bundle is moved into its own per-dataset
    subdirectory ``queue/processing/<dataset_id>/`` so the transformer
    can write output under any name (including the source's own) without
    clobbering the source. The source bundle stays in that subdirectory
    until approve() promotes it (then the whole subdirectory moves to
    ``queue/archived/<dataset_id>/``).

    **VTPK dispatch:** ``.vtpk`` files in ``queue/incoming/`` follow a
    separate path — they don't create new catalogue rows, they attach
    to an existing ``vector-tile-layer`` row by file stem (matching
    ``parks.vtpk`` to ``parks.gpkg``'s row). The actual ingest is
    handled by :func:`pipeline.agol_vtpk.ingest_one_vtpk`. To enable
    VTPK ingest, callers must supply ``library_root`` and ``db_path``;
    without them, ``.vtpk`` files are left in the queue and the scan
    summary will surface no VTPK results. CLI always passes both.
    """
    incoming_dir.mkdir(parents=True, exist_ok=True)
    processing_dir.mkdir(parents=True, exist_ok=True)
    rejected_dir.mkdir(parents=True, exist_ok=True)

    pending_path = processing_dir / pending_sheet.PENDING_FILENAME
    new_rows: list[dict[str, Any]] = []
    accepted = 0
    rejected = 0

    # Pre-scan: which .vtpk stems are currently in queue/incoming/?
    # If a GPKG's stem matches a VTPK in the queue, scan treats them
    # as a pair — the VTPK travels with the GPKG bundle into
    # processing/<dataset_id>/, and the pending row's agol_format
    # pre-fill flips from feature-layer to vector-tile-layer. The
    # VTPK then lands at library/vtpk/<target_stem>.vtpk as part of
    # approve, so the steward doesn't need a second `y2y ingest`
    # call to attach it. See agol_vtpk.ingest_one_vtpk for the
    # match-against-existing-catalogue-row path (used for VTPKs
    # whose paired GPKG isn't in this scan).
    vtpk_paths_by_stem: dict[str, Path] = {}
    for entry in sorted(incoming_dir.iterdir()):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        if entry.suffix.lower() == ".vtpk":
            vtpk_paths_by_stem[entry.stem] = entry
    paired_vtpk_stems: set[str] = set()

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

        # GPKG + matching-stem VTPK pair detection.
        paired_vtpk = (
            vtpk_paths_by_stem.get(entry.stem)
            if metadata.source_format == "GeoPackage"
            else None
        )
        if paired_vtpk is not None:
            bundle = list(bundle) + [paired_vtpk]
            paired_vtpk_stems.add(entry.stem)

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
        new_rows.append(_build_row(
            primary_in_staging, metadata,
            dataset_id=dataset_id,
            paired_with_vtpk=bool(paired_vtpk),
        ))
        accepted += 1

    if new_rows:
        pending_sheet.append_pending(pending_path, new_rows)

    # --- VTPK dispatch (for unpaired VTPKs only) ---
    # candidate_paths above only yields recognised source-data
    # extensions (.gpkg / .tif / .shp / .geojson / .kml / .kmz);
    # .vtpk falls through. VTPKs that paired with an in-scan GPKG
    # were already moved into processing/<dataset_id>/ above and
    # are handled by approve. The remaining .vtpks (no matching
    # GPKG in this scan) attach to existing active catalogue rows.
    vtpk_results: list[agol_vtpk.IngestVtpkResult] = []
    if library_root is not None and db_path is not None:
        for entry in sorted(incoming_dir.iterdir()):
            if not entry.is_file():
                continue
            if entry.name.startswith("."):
                continue
            if entry.suffix.lower() != ".vtpk":
                continue
            if entry.stem in paired_vtpk_stems:
                # Already staged with its paired GPKG; approve()
                # will promote it to library/vtpk/.
                continue
            vtpk_results.append(
                agol_vtpk.ingest_one_vtpk(
                    entry, library_root, db_path, actor=actor,
                )
            )

    return ScanResult(
        pending_path=pending_path,
        accepted=accepted,
        rejected=rejected,
        vtpk_results=tuple(vtpk_results),
    )


# === Phase 3: approve =================================================

def approve(
    processing_dir: Path,
    library_root: Path,
    db_path: Path,
    *,
    actor: str,
    archived_dir: Path | None = None,
    auto_push: bool = False,
) -> ApproveResult:
    """Phase 3: transform → validate → snapshot → promote → archive source.

    ``db_path`` points at ``inventory/inventory.db`` (the SQLite
    catalogue). The inventory and changelog are both inside it.

    When ``auto_push`` is true, each successfully-promoted row fires
    a best-effort AGOL push via :func:`agol_sync.try_auto_push` —
    AGOL failures never block promotion (the catalogue is the source
    of truth). The CLI's ``y2y ingest --approve`` sets this to True
    by default; tests and library callers leave it False.
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
    # Rev 3: track newly-approved VTL rows that don't yet have a
    # VTPK on disk so the CLI can print a reminder block. We append
    # AFTER successful promotion so failed/skipped rows aren't
    # spuriously flagged.
    vtpk_reminders: list[VtpkReminder] = []

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
            _promote(
                row, target_path, library_root, db_path, actor=actor,
            )
            # If a VTPK was paired with this GPKG at scan time, it's
            # now in processing/<dataset_id>/. Promote it to
            # library/vtpk/ BEFORE _archive_source moves the staging
            # contents to archived/. Uses the TARGET stem
            # (target_path.stem) so a steward rename via
            # target_filename pairs cleanly with the catalogue
            # row's file_path stem at push time.
            _maybe_promote_paired_vtpk(
                row, processing_dir, library_root, db_path,
                target_stem=target_path.stem, actor=actor,
            )
            _archive_source(row, processing_dir, archived_dir)
            promoted += 1
            # Rev 3: if this row is targeted for VTL but doesn't
            # have a VTPK yet at the canonical location, flag it for
            # the steward. We re-query the catalogue after promotion
            # so the check sees the canonical file_path that
            # _promote wrote to the DB (the pending-sheet row dict's
            # file_path is None until _promote sets it on the DB
            # copy). Pair-promoted VTPKs land at the canonical path
            # before this check runs, so no reminder fires for them.
            fresh_row = inventory_manager.get_dataset(
                db_path, str(row["dataset_id"]),
            )
            if fresh_row is not None:
                reminder = _vtpk_reminder_for_row(fresh_row, library_root)
                if reminder is not None:
                    vtpk_reminders.append(reminder)
            # AGOL auto-sync: best-effort push of the freshly-approved
            # row. Fires AFTER the paired VTPK (if any) has been
            # placed at library/vtpk/<stem>.vtpk so VTL pushes find
            # what they need. For rows with a missing VTPK,
            # try_auto_push falls through silently — the row stays
            # 'unpublished' and the next reconcile picks it up once
            # the steward drops the VTPK in queue/incoming/.
            if auto_push and fresh_row is not None:
                from . import agol_sync
                agol_sync.try_auto_push(
                    db_path, str(row["dataset_id"]),
                    library_root=library_root, actor=actor,
                    trigger="ingest-approve",
                )
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
        vtpk_reminders=tuple(vtpk_reminders),
    )


def _maybe_promote_paired_vtpk(
    row: dict[str, Any],
    processing_dir: Path,
    library_root: Path,
    db_path: Path,
    *,
    target_stem: str,
    actor: str,
) -> None:
    """Move a paired VTPK from staging to ``library/vtpk/`` if present.

    If scan() detected a ``<stem>.vtpk`` alongside a GPKG in
    ``queue/incoming/``, the VTPK was moved into
    ``queue/processing/<dataset_id>/`` together with the GPKG
    bundle. After ``_promote`` has landed the canonical GPKG in
    ``library/spatial/``, this helper finishes the pair by moving
    the VTPK to ``library/vtpk/<target_stem>.vtpk`` and writing
    the matching ``.sha256`` sidecar + an ``ingest-vtpk``
    changelog entry.

    The target stem comes from the catalogue row's ``file_path``
    (set on the row dict by ``_promote``), so a steward who
    renamed via ``target_filename`` in pending.xlsx still ends up
    with a correctly-paired VTPK at the canonical location.

    No-op if no ``.vtpk`` is in the staging dir — the common case
    for non-VTL rows or VTL rows whose VTPK comes in a later
    ``y2y ingest`` call.

    Failures are surfaced as exceptions so the caller can route
    the row to the failed bucket. (Doing this AFTER _promote
    succeeded means we've already mutated the catalogue; a failure
    here means the catalogue row exists but its VTPK didn't make
    it to the canonical location. Reconcile's vtpk_missing finding
    will then surface the steward action.)
    """
    dataset_id = str(row["dataset_id"])
    staging_dir = processing_dir / dataset_id
    if not staging_dir.exists():
        return
    candidate_vtpks = list(staging_dir.glob("*.vtpk"))
    if not candidate_vtpks:
        return
    if len(candidate_vtpks) > 1:
        # Surface as a warning via changelog but don't fail. The
        # pair-detection logic only adds one VTPK per scan; >1
        # implies an out-of-band edit.
        names = ", ".join(p.name for p in candidate_vtpks)
        inventory_manager.append_changelog(
            db_path,
            timestamp=utils.utc_now_iso(),
            action="metadata",
            dataset_id=dataset_id,
            actor=actor,
            path=row.get("file_path"),
            field_changed="vtpk",
            old_value=None,
            new_value=None,
            detail=(
                f"multiple .vtpk files found in staging dir "
                f"({names}); skipped paired-VTPK promotion. "
                f"Steward must `y2y ingest` after approval."
            ),
        )
        return

    src_vtpk = candidate_vtpks[0]
    # target_stem is passed in by the caller (approve loop) from
    # target_path.stem — i.e., the post-transformation canonical
    # filename's stem. Steward renames via target_filename in
    # pending.xlsx are honored: the VTPK lands under the renamed
    # stem so it pairs with the catalogue row's file_path stem at
    # push time.
    target_dir = library_root.parent / "vtpk"
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / f"{target_stem}.vtpk"
    # Replace any prior file at dest cleanly (a re-ingest scenario).
    if dest.exists():
        dest.unlink()
    sidecar = dest.with_suffix(dest.suffix + ".sha256")
    if sidecar.exists():
        sidecar.unlink()
    digest = utils.sha256_file(src_vtpk)
    src_vtpk.rename(dest)
    sidecar.write_text(digest, encoding="utf-8")

    inventory_manager.append_changelog(
        db_path,
        timestamp=utils.utc_now_iso(),
        action="metadata",
        dataset_id=dataset_id,
        actor=actor,
        path=row.get("file_path"),
        field_changed="vtpk",
        old_value=None,
        new_value=digest,
        detail=(
            f"ingested paired VTPK to "
            f"{dest.relative_to(library_root.parent)} (sha256={digest})"
        ),
    )


def _vtpk_reminder_for_row(
    row: dict[str, Any], library_root: Path,
) -> VtpkReminder | None:
    """Return a VtpkReminder when ``row`` needs a VTPK and lacks one.

    Conditions: ``agol_format == 'vector-tile-layer'`` AND the
    expected VTPK isn't present at the canonical location.
    Otherwise returns ``None``.
    """
    if (row.get("agol_format") or "") != "vector-tile-layer":
        return None
    if not row.get("file_path"):
        return None
    if agol_vtpk.vtpk_present(row, library_root):
        return None
    return VtpkReminder(
        dataset_id=str(row.get("dataset_id", "?")),
        gpkg_relative_path=str(row["file_path"]),
        expected_vtpk_path=agol_vtpk.resolve_vtpk_path(row, library_root),
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
    paired_with_vtpk: bool = False,
) -> dict[str, Any]:
    """Compose a fresh pending row at scan time. No intrinsic snapshot yet.

    ``paired_with_vtpk`` flips the agol_format pre-fill from the
    format-default (feature-layer for vectors / imagery-layer for
    rasters) to ``vector-tile-layer`` when scan detected a matching
    ``.vtpk`` in queue/incoming/ alongside this source. Steward
    still overrides freely in pending.xlsx.
    """
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
        # Default AGOL publish target derived from source format
        # (vector → feature-layer; raster → imagery-layer). Steward
        # can override to vector-tile-layer in the pending sheet for
        # vector data they want delivered as a cached tile service.
        # If a matching .vtpk was paired during scan
        # (paired_with_vtpk=True), the default flips to
        # vector-tile-layer up front. See DESIGN.md §15.
        "agol_format": (
            "vector-tile-layer" if paired_with_vtpk
            else ("feature-layer" if meta.is_vector else "imagery-layer")
        ),
        # error sink
        pending_sheet.ERROR_COLUMN: None,
    }


# Required-non-empty fields a steward must fill before approval.
_REQUIRED_NON_EMPTY = (
    "title", "category", "format",
    pending_sheet.TARGET_FILENAME_COLUMN,
    "status", "data_steward",
    "summary", "description", "tags", "terms_of_use", "acknowledgements",
    # agol_format is pre-filled by _build_row from format (vector →
    # feature-layer, raster → imagery-layer); the steward may
    # override to vector-tile-layer. Empty values shouldn't reach
    # the catalogue — the column has a CHECK enum + downstream push
    # uses it to pick the publish path.
    "agol_format",
)

# Allowed values for agol_format. Mirrors the CHECK constraint
# defined in pipeline/schema.sql.
_AGOL_FORMAT_VALUES = frozenset({
    "feature-layer", "vector-tile-layer", "imagery-layer",
})


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

    # agol_format enum (matches schema.sql CHECK). The non-empty
    # check is handled by _REQUIRED_NON_EMPTY; this rejects values
    # that are present but invalid.
    agol_fmt = row.get("agol_format")
    if agol_fmt and agol_fmt not in _AGOL_FORMAT_VALUES:
        errors.append(
            f"agol_format {agol_fmt!r} is not allowed; "
            f"must be one of {sorted(_AGOL_FORMAT_VALUES)}"
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

    # NOTE: AGOL auto-push for newly-approved rows happens in
    # approve() AFTER _maybe_promote_paired_vtpk lands the VTPK at
    # its canonical location — pushing a VTL row from here would
    # fail because the VTPK isn't on disk yet.
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
