"""Vector Tile Package (VTPK) ingest + path resolution.

Y2Y's Vector Tile Layer (VTL) publishing model splits the work
between the steward (who manually builds a ``.vtpk`` in ArcGIS
Pro's UI) and this pipeline (which ingests the resulting file
from ``queue/incoming/`` into a canonical library location, then
uploads + publishes it to AGOL).

The arcpy-driven automatic-VTPK-build path was tried in earlier
revisions but proved fragile across ArcGIS Pro upgrades (Python
ABI mismatches, license-init issues, COM file-handle locking, the
absence of a usable in-process Map context, etc.). Rev 3 (this
revision) drops arcpy entirely from the pipeline: Pro is only used
manually by the steward to produce the ``.vtpk``; everything that
follows runs from pure Python with the ``arcgis`` SDK.

Module responsibilities:

* :func:`resolve_vtpk_path` — given a catalogue row and the spatial
  library root, return the canonical VTPK location under
  ``library/vtpk/``.
* :func:`vtpk_present` / :func:`vtpk_stale` — quick boolean probes
  used by the push pre-flight and by ``reconcile`` to surface
  missing or out-of-date VTPKs as steward action items.
* :func:`ingest_one_vtpk` — handles a single ``.vtpk`` file in
  ``queue/incoming/``: validates it's a real VTPK (ZIP container
  signature), matches by file stem to a ``vector-tile-layer`` row
  in the catalogue, moves to ``library/vtpk/``, writes a
  ``.sha256`` sidecar, appends an ``ingest-vtpk`` changelog entry.
* :func:`read_vtpk_checksum` — reads from the sidecar if present,
  computes if not.

This module is called by :func:`pipeline.ingest.scan` for every
``.vtpk`` discovered in the queue (the scan dispatches on file
extension), and by :func:`pipeline.agol_sync._publish_vector_tile_layer`
during a VTL push. There is no dedicated CLI entry point — VTPK
ingest piggy-backs on the existing ``y2y ingest scan``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, NamedTuple

from . import inventory_manager, utils

# Sidecar suffix appended to the .vtpk filename for the checksum
# file (matches the existing pattern used by agol_thumbnails.py).
_CHECKSUM_SIDECAR_SUFFIX = ".sha256"

# Subdirectory under ``library/`` where ingested VTPKs live, sibling
# to ``library/spatial/``. Created on first ingest if absent.
_VTPK_LIBRARY_SUBDIR = "vtpk"

# ZIP local file header magic. VTPK is a .zip container; we sniff
# the first four bytes during ingest to reject anything that
# doesn't look like a real package before we touch the catalogue.
_ZIP_MAGIC = b"PK\x03\x04"


class IngestVtpkResult(NamedTuple):
    """Outcome of attempting to ingest a single ``.vtpk`` file.

    Returned by :func:`ingest_one_vtpk` so callers (currently
    :func:`pipeline.ingest.scan`) can aggregate results across the
    queue without losing per-file context.

    Fields:
        status: One of ``"moved"`` (success), ``"unmatched"`` (no
            catalogue row), ``"ambiguous"`` (multiple matches),
            ``"target_mismatch"`` (match found but its
            ``agol_format`` is not ``"vector-tile-layer"``), or
            ``"invalid"`` (the file is not a valid VTPK).
        vtpk_path: Path the file occupied at ingest time. For
            ``"moved"`` this is the source path in ``queue/``; the
            destination is in ``destination``.
        dataset_id: The matched row's ``dataset_id`` if status is
            ``"moved"`` or ``"target_mismatch"``; otherwise ``None``.
        destination: Where the file was moved to. Only populated
            for status ``"moved"``.
        message: Human-readable explanation, suitable for inclusion
            in the scan summary output.
    """

    status: str
    vtpk_path: Path
    dataset_id: str | None
    destination: Path | None
    message: str


# ----------------------------------------------------------------------------
# Path resolution + invariant probes
# ----------------------------------------------------------------------------

def resolve_vtpk_path(row: dict[str, Any], library_root: Path) -> Path:
    """Return the canonical VTPK path for ``row``.

    The catalogue row's ``file_path`` is relative to
    ``library_root`` (the spatial subtree), e.g.
    ``"Land_Designations_Tenure/parks.gpkg"``. The corresponding
    VTPK lives under a sibling ``vtpk/`` subtree, flat (no category
    folders), keyed by the GPKG's file stem:

        library/spatial/Land_Designations_Tenure/parks.gpkg
        library/vtpk/parks.vtpk

    Args:
        row: A catalogue row dict carrying ``file_path``.
        library_root: The spatial-typed library root
            (``library/spatial``). The VTPK subtree is at
            ``library_root.parent / "vtpk"``.

    Returns:
        Absolute path to where the VTPK should live. **Whether the
        file actually exists is the caller's concern** — use
        :func:`vtpk_present` to check.

    Raises:
        ValueError: ``row`` has no ``file_path``.
    """
    file_path = row.get("file_path")
    if not file_path:
        raise ValueError(
            f"row has no file_path; cannot resolve VTPK path: "
            f"dataset_id={row.get('dataset_id')!r}"
        )
    stem = Path(file_path).stem
    return library_root.parent / _VTPK_LIBRARY_SUBDIR / f"{stem}.vtpk"


def vtpk_present(row: dict[str, Any], library_root: Path) -> bool:
    """Quick probe: does the row's expected VTPK exist on disk?"""
    try:
        return resolve_vtpk_path(row, library_root).exists()
    except ValueError:
        return False


def vtpk_stale(row: dict[str, Any], library_root: Path) -> bool:
    """Probe: is the row's VTPK older than its source GPKG?

    Returns ``False`` when the VTPK doesn't exist (the
    ``vtpk_present`` check covers that case separately) and also
    when there's no source GPKG to compare against. Returns
    ``True`` only when both files exist and the GPKG's mtime is
    strictly newer than the VTPK's. Stewards rebuild the VTPK in
    Pro after source changes; this probe is how reconcile catches
    forgotten rebuilds.
    """
    try:
        vtpk_path = resolve_vtpk_path(row, library_root)
    except ValueError:
        return False
    if not vtpk_path.exists():
        return False
    gpkg_path = library_root / (row.get("file_path") or "")
    if not gpkg_path.exists():
        return False
    return gpkg_path.stat().st_mtime > vtpk_path.stat().st_mtime


def read_vtpk_checksum(vtpk_path: Path) -> str:
    """Return the VTPK's SHA-256, reading the sidecar if present.

    On first call (or whenever the sidecar is missing) the
    checksum is computed by streaming the file, then written to
    the sidecar for future calls. Keeps push() and reconcile fast
    even for multi-MB VTPKs.
    """
    sidecar = vtpk_path.with_suffix(vtpk_path.suffix + _CHECKSUM_SIDECAR_SUFFIX)
    if sidecar.exists():
        cached = sidecar.read_text(encoding="utf-8").strip()
        if cached:
            return cached
    digest = utils.sha256_file(vtpk_path)
    sidecar.write_text(digest, encoding="utf-8")
    return digest


# ----------------------------------------------------------------------------
# Ingest
# ----------------------------------------------------------------------------

def ingest_one_vtpk(
    vtpk_path: Path,
    library_root: Path,
    db_path: Path,
    *,
    actor: str,
) -> IngestVtpkResult:
    """Move one queued VTPK into the canonical library location.

    The pipeline's ``y2y ingest scan`` walks ``queue/incoming/``
    and calls this function for every file whose extension is
    ``.vtpk``. The expected steward workflow is:

    1. Steward builds the VTPK in ArcGIS Pro's "Share As Vector
       Tile Package" dialog, saves it with the same stem as the
       source GPKG (e.g., ``parks.vtpk`` for ``parks.gpkg``).
    2. Steward drops the file in ``queue/incoming/``.
    3. ``y2y ingest scan`` discovers it and calls this function.

    This function:

    * Validates the file is a real VTPK (ZIP container) by sniffing
      the first four bytes. A spurious file with a ``.vtpk``
      extension is rejected without touching the catalogue.
    * Matches the file to a catalogue row by file stem:
      ``status='active'`` AND ``agol_format='vector-tile-layer'``
      AND ``file_path`` ends with ``<stem>.gpkg``.
    * On a single clean match: computes SHA-256, moves the file to
      ``library/vtpk/<stem>.vtpk``, writes the ``.sha256`` sidecar,
      appends an ``ingest-vtpk`` changelog entry capturing the
      checksum.
    * On 0 / >1 / wrong-target matches: leaves the file in the
      queue and returns a non-``moved`` status. The scan summary
      surfaces these so the steward can fix the filename or the
      catalogue's ``agol_format`` and re-run.

    Args:
        vtpk_path: Absolute path to the queued ``.vtpk`` file.
        library_root: ``<project>/library/spatial/`` — the typed
            library root. The VTPK subtree is a sibling at
            ``library_root.parent / "vtpk"``.
        db_path: Path to ``inventory.db``.
        actor: Recorded as the changelog actor.

    Returns:
        :class:`IngestVtpkResult` describing the outcome.
    """
    # --- validation: file actually exists ---
    if not vtpk_path.exists() or not vtpk_path.is_file():
        return IngestVtpkResult(
            status="invalid", vtpk_path=vtpk_path,
            dataset_id=None, destination=None,
            message=f"file does not exist or is not a regular file",
        )

    # --- validation: looks like a real VTPK (ZIP) ---
    try:
        with vtpk_path.open("rb") as f:
            magic = f.read(4)
    except OSError as exc:
        return IngestVtpkResult(
            status="invalid", vtpk_path=vtpk_path,
            dataset_id=None, destination=None,
            message=f"could not read header: {exc}",
        )
    if magic != _ZIP_MAGIC:
        return IngestVtpkResult(
            status="invalid", vtpk_path=vtpk_path,
            dataset_id=None, destination=None,
            message=(
                f"not a valid VTPK (ZIP signature missing; got "
                f"{magic!r}). VTPKs are zip containers."
            ),
        )

    # --- match by file stem ---
    stem = vtpk_path.stem
    matches = _find_rows_by_gpkg_stem(db_path, stem)
    if not matches:
        return IngestVtpkResult(
            status="unmatched", vtpk_path=vtpk_path,
            dataset_id=None, destination=None,
            message=(
                f"no active catalogue row found with a .gpkg "
                f"file_path matching stem {stem!r}. The VTPK "
                f"filename must match the GPKG filename "
                f"(e.g., {stem}.vtpk pairs with {stem}.gpkg)."
            ),
        )
    if len(matches) > 1:
        ids = ", ".join(r["dataset_id"] for r in matches)
        return IngestVtpkResult(
            status="ambiguous", vtpk_path=vtpk_path,
            dataset_id=None, destination=None,
            message=(
                f"multiple active rows share GPKG stem {stem!r}: "
                f"{ids}. Cannot infer which one this VTPK belongs "
                f"to. Use unique filenames or drop the VTPK only "
                f"after the duplicate is resolved."
            ),
        )

    row = matches[0]
    if row.get("agol_format") != "vector-tile-layer":
        return IngestVtpkResult(
            status="target_mismatch", vtpk_path=vtpk_path,
            dataset_id=row["dataset_id"], destination=None,
            message=(
                f"row {row['dataset_id']!r} matched by stem but its "
                f"agol_format is {row.get('agol_format')!r}, not "
                f"'vector-tile-layer'. To accept this VTPK, run "
                f"`y2y update {row['dataset_id']} "
                f"--set agol_format=vector-tile-layer` first."
            ),
        )

    # --- move + sidecar + changelog ---
    dest = resolve_vtpk_path(row, library_root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = utils.sha256_file(vtpk_path)
    # Replace any prior VTPK at the destination (re-ingest is a
    # refresh; the prior file's checksum is captured in the
    # previous changelog entry, so we don't need to preserve it).
    if dest.exists():
        dest.unlink()
    sidecar = dest.with_suffix(dest.suffix + _CHECKSUM_SIDECAR_SUFFIX)
    if sidecar.exists():
        sidecar.unlink()
    vtpk_path.rename(dest)
    sidecar.write_text(digest, encoding="utf-8")

    # Audit entry. Using action='metadata' for the changelog because
    # it captures the steward-visible state change ("a VTPK now
    # exists for this row") without inventing a new action type —
    # the structured note + field_changed='vtpk' is what makes it
    # identifiable.
    inventory_manager.append_changelog(
        db_path,
        timestamp=utils.utc_now_iso(),
        action="metadata",
        dataset_id=row["dataset_id"],
        actor=actor,
        path=row.get("file_path"),
        field_changed="vtpk",
        old_value=None,
        new_value=digest,
        detail=(
            f"ingested VTPK to {dest.relative_to(library_root.parent)} "
            f"(sha256={digest})"
        ),
    )

    return IngestVtpkResult(
        status="moved", vtpk_path=vtpk_path,
        dataset_id=row["dataset_id"], destination=dest,
        message=(
            f"moved to library/vtpk/{dest.name} for dataset "
            f"{row['dataset_id']} (sha256={digest[:12]}…)"
        ),
    )


# ----------------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------------

def _find_rows_by_gpkg_stem(
    db_path: Path, stem: str,
) -> list[dict[str, Any]]:
    """Return active rows whose ``file_path`` ends with ``<stem>.gpkg``.

    Used by the VTPK ingest to find the catalogue row a queued
    file belongs to. The match is a SQL LIKE on the file_path
    suffix; the row's ``agol_format`` is checked by the caller
    (we report a clear ``target_mismatch`` rather than silently
    ignoring rows that aren't VTL targets).

    Uses ``pipeline.db.connect`` rather than a raw sqlite3 call so
    a brand-new DB file gets the schema applied lazily. (Production
    catalogues always have the schema in place; tests sometimes
    don't, and we don't want to crash with "no such table".)
    """
    from . import db as _db
    with _db.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM datasets "
            "WHERE status = 'active' AND file_path LIKE ?",
            (f"%{stem}.gpkg",),
        ).fetchall()
    return [dict(r) for r in rows]
