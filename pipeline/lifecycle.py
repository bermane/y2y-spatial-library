"""Post-ingest lifecycle operations on inventory rows.

Four operations:

- ``update()``    Change non-locked fields on an existing row.
- ``rename()``    Move a file within library/ and record the new path.
- ``tombstone()`` Mark a row removed (status='tombstoned') and delete the file.
- ``refresh()``   Re-stat a library file after an in-place edit and update
                  the inventory's intrinsic snapshot to match.

Each appends an entry to the changelog with the corresponding action
(``update`` / ``rename`` / ``remove`` / ``refresh``). The first three
never touch filesystem-derived locked columns (checksum, size, mtime,
crs, bbox); ``refresh`` is the *only* operation that updates them, and
only by recomputing them from the current file on disk — never from
arbitrary steward input.

Post-migration to SQLite (2026-04-29): all operations take a single
``db_path`` (the path to ``inventory.db``) instead of separate
``inventory_path`` and ``changelog_path``. Both tables are in the same
database. Lifecycle ops use row-level UPDATEs via
:func:`inventory_manager.update_dataset` rather than the old
load-mutate-save-all pattern.

See DESIGN.md §7 (column roles) and ``schema.sql`` (valid actions).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import inventory_manager, taxonomy, utils
from .validators import validate_all
from .validators.naming import validate_naming


# Fields permitted to change via update(). Anything outside this set
# either lives in the filesystem (locked) or requires a file move
# (which goes through rename()). Note ``notes`` was renamed to
# ``internal_notes`` in the SQLite schema.
UPDATABLE_FIELDS: frozenset[str] = frozenset({
    "title", "status",
    "classification",  # raster only; see DESIGN.md §11
    "data_steward",
    "summary", "description", "tags", "terms_of_use", "acknowledgements",
    "agol_item_id", "internal_notes",
    # AGOL publish-target intent — steward changes their mind about
    # how a dataset should be published (e.g., promoting a large
    # vector from feature-layer to vector-tile-layer). See DESIGN.md
    # §15.
    "agol_format",
})

# Status values reachable via update(). Tombstoning is its own command
# because it has filesystem side effects.
_UPDATE_STATUSES: frozenset[str] = frozenset({"active", "deprecated"})


class LifecycleError(Exception):
    """Raised for any lifecycle-operation precondition failure."""


# --- helpers ------------------------------------------------------------

def _require_row(db_path: Path, dataset_id: str) -> dict[str, Any]:
    row = inventory_manager.get_dataset(db_path, dataset_id)
    if row is None:
        raise LifecycleError(f"dataset_id {dataset_id!r} not found in inventory")
    return row


# --- update -------------------------------------------------------------

def update(
    db_path: Path,
    *,
    dataset_id: str,
    fields: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    """Update specified fields on an inventory row.

    Only fields in :data:`UPDATABLE_FIELDS` may be changed. Locked
    columns (checksum, size, mtime, etc.) and movement-bound columns
    (file_path, category, subcategory) are rejected with a clear error.

    Returns the updated row dict (re-read after the change).
    """
    if not fields:
        raise LifecycleError("no fields supplied to update")

    bad = set(fields) - UPDATABLE_FIELDS
    if bad:
        raise LifecycleError(
            f"cannot update these fields via 'update': {sorted(bad)}. "
            f"Allowed: {sorted(UPDATABLE_FIELDS)}. "
            f"Use 'rename' to change file_path/category/subcategory; "
            f"locked columns are filesystem-derived and immutable."
        )

    if "status" in fields and fields["status"] not in _UPDATE_STATUSES:
        raise LifecycleError(
            f"status='{fields['status']}' not allowed via 'update' "
            f"(allowed: {sorted(_UPDATE_STATUSES)}). Use 'tombstone' to soft-delete."
        )

    target = _require_row(db_path, dataset_id)
    if target.get("status") == "tombstoned":
        raise LifecycleError(f"dataset_id {dataset_id!r} is tombstoned; updates not allowed")

    # Compute the diff so we can produce a useful changelog entry and
    # silently no-op when nothing actually changed.
    changes: list[tuple[str, Any, Any]] = []
    actual_updates: dict[str, Any] = {}
    for k, v in fields.items():
        old = target.get(k)
        if old != v:
            changes.append((k, old, v))
            actual_updates[k] = v

    if not changes:
        return target  # no-op; nothing to log or write

    actual_updates["date_modified"] = utils.utc_now_iso()
    inventory_manager.update_dataset(db_path, dataset_id, actual_updates)

    detail = " | ".join(f"{k}: {old!r} → {new!r}" for k, old, new in changes)
    inventory_manager.append_changelog(
        db_path,
        timestamp=utils.utc_now_iso(),
        action="update",
        dataset_id=dataset_id,
        actor=actor,
        path=str(target.get("file_path") or "—"),
        detail=detail,
    )
    return _require_row(db_path, dataset_id)


# --- rename -------------------------------------------------------------

def rename(
    db_path: Path,
    library_root: Path,
    *,
    dataset_id: str,
    new_path: str,
    actor: str,
) -> dict[str, Any]:
    """Move a file within library/ and update the inventory's ``file_path``.

    ``new_path`` is library-relative using folder-name conventions, e.g.
    ``"Water/streams_v2.gpkg"`` or ``"Species/Caribou/dens_2024.gpkg"``.
    Its filename must pass the naming validator. ``category`` (and
    ``subcategory`` when present) in the inventory are set to the
    *display* names corresponding to those folders.

    Handles three filesystem situations:

    - file at ``old_path``, nothing at ``new_path``: actively move the file.
    - file at ``new_path``, nothing at ``old_path``: record the rename
      that the steward already did manually (no filesystem action).
      This is the normal mode for ``y2y reconcile --fix-renames``.
    - file at both, or at neither: raise — operator must resolve.
    """
    new_path_obj = Path(new_path)

    ok, reason = validate_naming(new_path_obj)
    if not ok:
        raise LifecycleError(f"new path {new_path!r} fails naming convention: {reason}")

    parts = new_path_obj.parts
    if len(parts) < 2:
        raise LifecycleError(
            f"new path {new_path!r} must include a category folder, "
            f"e.g. 'Water/streams_v2.gpkg'."
        )
    new_category_folder = parts[0]
    new_subcategory_folder = parts[1] if len(parts) >= 3 else None

    if new_category_folder not in taxonomy.FOLDER_TO_CATEGORY:
        raise LifecycleError(
            f"category folder {new_category_folder!r} (from new path) is not "
            f"one of the {len(taxonomy.CATEGORIES)} canonical category folders."
        )
    new_category_display = taxonomy.FOLDER_TO_CATEGORY[new_category_folder]

    new_subcategory_display: str | None = None
    if new_subcategory_folder is not None:
        sub_map = taxonomy.SUBCATEGORY_FROM_FOLDER.get(new_category_display, {})
        if new_subcategory_folder not in sub_map:
            allowed = taxonomy.SUBCATEGORY_FOLDERS.get(new_category_display)
            if allowed:
                raise LifecycleError(
                    f"subcategory folder {new_subcategory_folder!r} is not valid "
                    f"for category {new_category_display!r}; allowed: "
                    f"{sorted(allowed.values())}."
                )
            raise LifecycleError(
                f"category {new_category_display!r} admits no subcategory."
            )
        new_subcategory_display = sub_map[new_subcategory_folder]

    target = _require_row(db_path, dataset_id)
    if target.get("status") == "tombstoned":
        raise LifecycleError(f"dataset_id {dataset_id!r} is tombstoned; renames not allowed")

    old_path = str(target.get("file_path") or "")
    if old_path == new_path:
        raise LifecycleError(f"new path is identical to current path: {new_path!r}")

    old_full = library_root / old_path
    new_full = library_root / new_path
    old_exists = old_full.exists()
    new_exists = new_full.exists()

    if old_exists and new_exists:
        raise LifecycleError(
            f"both old ({old_path!r}) and new ({new_path!r}) exist on disk; "
            f"resolve manually before running rename."
        )
    if not old_exists and not new_exists:
        raise LifecycleError(
            f"neither old ({old_path!r}) nor new ({new_path!r}) exists on disk."
        )

    if old_exists:
        new_full.parent.mkdir(parents=True, exist_ok=True)
        old_full.rename(new_full)
        action_note = "moved file"
    else:
        action_note = "file already at new path; recording inventory only"

    inventory_manager.update_dataset(
        db_path,
        dataset_id,
        {
            "file_path": new_path,
            "category": new_category_display,
            "subcategory": new_subcategory_display,
            "date_modified": utils.utc_now_iso(),
        },
    )

    inventory_manager.append_changelog(
        db_path,
        timestamp=utils.utc_now_iso(),
        action="rename",
        dataset_id=dataset_id,
        actor=actor,
        path=new_path,
        detail=f"{action_note}: {old_path!r} → {new_path!r}",
        field_changed="file_path",
        old_value=old_path,
        new_value=new_path,
    )
    return _require_row(db_path, dataset_id)


# --- tombstone ----------------------------------------------------------

def tombstone(
    db_path: Path,
    library_root: Path,
    *,
    dataset_id: str,
    actor: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Mark a row tombstoned and delete the file from library/.

    The row stays in the inventory permanently as an audit record.
    ``dataset_id`` remains reserved. Reconcile expects the file to be
    absent and will flag a violation if it reappears.

    If the file is already absent (manual deletion), the operation
    proceeds and notes that fact in the changelog.
    """
    target = _require_row(db_path, dataset_id)
    if target.get("status") == "tombstoned":
        raise LifecycleError(f"dataset_id {dataset_id!r} is already tombstoned")

    file_path = str(target.get("file_path") or "")
    full_path = (library_root / file_path) if file_path else None

    if full_path and full_path.exists():
        full_path.unlink()
        action_note = "deleted file from library/"
    else:
        action_note = "file already absent from library/"

    inventory_manager.update_dataset(
        db_path,
        dataset_id,
        {"status": "tombstoned", "date_modified": utils.utc_now_iso()},
    )

    detail = f"{action_note}; dataset_id retained for audit"
    if reason:
        detail = f"{detail}. reason: {reason}"

    inventory_manager.append_changelog(
        db_path,
        timestamp=utils.utc_now_iso(),
        action="remove",
        dataset_id=dataset_id,
        actor=actor,
        path=file_path or "—",
        detail=detail,
        field_changed="status",
        old_value=str(target.get("status") or ""),
        new_value="tombstoned",
    )
    return _require_row(db_path, dataset_id)


# --- refresh ------------------------------------------------------------

# Snapshot fields recomputed from disk. Other locked columns
# (dataset_id, file_path, source_*, date_added) are not touched.
_REFRESH_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "checksum_sha256", "size_bytes", "mtime", "crs", "geographic_extent_bbox",
)


def refresh(
    db_path: Path,
    library_root: Path,
    *,
    dataset_id: str,
    actor: str,
) -> dict[str, Any]:
    """Re-stat the canonical file in library/ and update the inventory snapshot.

    Use after editing a library file in place — adding a vector field,
    recomputing attribute values, regenerating overviews, etc. — to
    bring the inventory's intrinsic snapshot up to date with the file
    on disk. Logs a ``refresh`` entry with the per-field diff.

    The file must still pass canonical validators: ``refresh`` will not
    accept drift that breaks format/CRS/naming compliance.

    No-op when nothing has changed.
    """
    target = _require_row(db_path, dataset_id)
    if target.get("status") == "tombstoned":
        raise LifecycleError(
            f"dataset_id {dataset_id!r} is tombstoned; refresh not allowed"
        )

    file_path = str(target.get("file_path") or "")
    if not file_path:
        raise LifecycleError(f"dataset_id {dataset_id!r} has no file_path")

    full_path = library_root / file_path
    if not full_path.exists():
        raise LifecycleError(
            f"file {file_path!r} not found in library/ — run `y2y reconcile` "
            f"to investigate the ghost before attempting refresh."
        )

    failures = validate_all(full_path)
    if failures:
        reasons = "; ".join(f"{check}: {reason}" for check, reason in failures)
        raise LifecycleError(
            f"file {file_path!r} fails canonical validators; refusing to "
            f"record bad state in the inventory. Fix the file first. "
            f"Failures: {reasons}"
        )

    new_size, new_mtime = utils.stat_signature(full_path)
    new_checksum = utils.sha256_file(full_path)
    if utils.is_vector(full_path):
        crs_obj = utils.read_vector_crs(full_path)
        bbox = utils.read_vector_bbox(full_path)
    else:
        crs_obj = utils.read_raster_crs(full_path)
        bbox = utils.read_raster_bbox(full_path)
    new_crs = utils.crs_to_authority_string(crs_obj) if crs_obj else None
    new_bbox = utils.bbox_to_string(bbox) if bbox else None

    new_snapshot = {
        "checksum_sha256": new_checksum,
        "size_bytes": new_size,
        "mtime": new_mtime,
        "crs": new_crs,
        "geographic_extent_bbox": new_bbox,
    }

    diffs: list[tuple[str, Any, Any]] = []
    for field in _REFRESH_SNAPSHOT_FIELDS:
        old = target.get(field)
        new = new_snapshot[field]
        if old != new:
            diffs.append((field, old, new))

    if not diffs:
        return target  # no-op

    update_payload = {field: new for field, _, new in diffs}
    update_payload["date_modified"] = utils.utc_now_iso()
    inventory_manager.update_dataset(db_path, dataset_id, update_payload)

    detail = " | ".join(f"{f}: {old!r} → {new!r}" for f, old, new in diffs)
    inventory_manager.append_changelog(
        db_path,
        timestamp=utils.utc_now_iso(),
        action="refresh",
        dataset_id=dataset_id,
        actor=actor,
        path=file_path,
        detail=f"snapshot refreshed from disk: {detail}",
    )
    return _require_row(db_path, dataset_id)
