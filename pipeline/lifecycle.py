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

See DESIGN.md §7 (column roles) and the changelog header (valid actions).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import inventory_manager, taxonomy, utils
from .validators import validate_all
from .validators.naming import validate_naming


# Fields permitted to change via update(). Anything outside this set
# either lives in the filesystem (locked) or requires a file move
# (which goes through rename()).
UPDATABLE_FIELDS: frozenset[str] = frozenset({
    "title", "status",
    "classification",  # raster only; see DESIGN.md §11
    "data_steward",
    "summary", "description", "tags", "terms_of_use", "acknowledgements",
    "agol_item_id", "notes",
})

# Status values reachable via update(). Tombstoning is its own command
# because it has filesystem side effects.
_UPDATE_STATUSES: frozenset[str] = frozenset({"active", "deprecated"})


class LifecycleError(Exception):
    """Raised for any lifecycle-operation precondition failure."""


# --- helpers ------------------------------------------------------------

def _find_row(rows: list[dict[str, Any]], dataset_id: str) -> dict[str, Any]:
    for r in rows:
        if r.get("dataset_id") == dataset_id:
            return r
    raise LifecycleError(f"dataset_id '{dataset_id}' not found in inventory")


# --- update -------------------------------------------------------------

def update(
    inventory_path: Path,
    changelog_path: Path,
    *,
    dataset_id: str,
    fields: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    """Update specified fields on an inventory row.

    Only fields in :data:`UPDATABLE_FIELDS` may be changed. Locked
    columns (checksum, size, mtime, etc.) and movement-bound columns
    (file_path, category, subcategory) are rejected with a clear error.

    Returns the updated row dict (in-memory snapshot after the change).
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

    rows = inventory_manager.load_inventory(inventory_path)
    target = _find_row(rows, dataset_id)

    if target.get("status") == "tombstoned":
        raise LifecycleError(f"dataset_id '{dataset_id}' is tombstoned; updates not allowed")

    changes: list[str] = []
    for k, v in fields.items():
        old = target.get(k)
        if old != v:
            target[k] = v
            changes.append(f"{k}: {old!r} → {v!r}")

    if not changes:
        return target  # no-op; nothing to log or save

    target["date_modified"] = utils.utc_now_date()
    inventory_manager.save_inventory(inventory_path, rows)

    inventory_manager.append_changelog(
        changelog_path,
        timestamp=utils.utc_now_iso(),
        action="update",
        dataset_id=dataset_id,
        actor=actor,
        path=str(target.get("file_path") or "—"),
        detail=" | ".join(changes),
    )
    return target


# --- rename -------------------------------------------------------------

def rename(
    inventory_path: Path,
    changelog_path: Path,
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
        raise LifecycleError(f"new path '{new_path}' fails naming convention: {reason}")

    parts = new_path_obj.parts
    if len(parts) < 2:
        raise LifecycleError(
            f"new path '{new_path}' must include a category folder, "
            f"e.g. 'Water/streams_v2.gpkg'."
        )
    new_category_folder = parts[0]
    new_subcategory_folder = parts[1] if len(parts) >= 3 else None

    if new_category_folder not in taxonomy.FOLDER_TO_CATEGORY:
        raise LifecycleError(
            f"category folder '{new_category_folder}' (from new path) is not "
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
                    f"subcategory folder '{new_subcategory_folder}' is not valid "
                    f"for category '{new_category_display}'; allowed: "
                    f"{sorted(allowed.values())}."
                )
            raise LifecycleError(
                f"category '{new_category_display}' admits no subcategory."
            )
        new_subcategory_display = sub_map[new_subcategory_folder]

    rows = inventory_manager.load_inventory(inventory_path)
    target = _find_row(rows, dataset_id)

    if target.get("status") == "tombstoned":
        raise LifecycleError(f"dataset_id '{dataset_id}' is tombstoned; renames not allowed")

    old_path = str(target.get("file_path") or "")
    if old_path == new_path:
        raise LifecycleError(f"new path is identical to current path: '{new_path}'")

    old_full = library_root / old_path
    new_full = library_root / new_path
    old_exists = old_full.exists()
    new_exists = new_full.exists()

    if old_exists and new_exists:
        raise LifecycleError(
            f"both old ('{old_path}') and new ('{new_path}') exist on disk; "
            f"resolve manually before running rename."
        )
    if not old_exists and not new_exists:
        raise LifecycleError(
            f"neither old ('{old_path}') nor new ('{new_path}') exists on disk."
        )

    if old_exists:
        # Active move: do it now.
        new_full.parent.mkdir(parents=True, exist_ok=True)
        old_full.rename(new_full)
        action_note = "moved file"
    else:
        # File already at new location (e.g., steward moved it manually).
        action_note = "file already at new path; recording inventory only"

    target["file_path"] = new_path
    target["category"] = new_category_display
    target["subcategory"] = new_subcategory_display
    target["date_modified"] = utils.utc_now_date()

    inventory_manager.save_inventory(inventory_path, rows)

    inventory_manager.append_changelog(
        changelog_path,
        timestamp=utils.utc_now_iso(),
        action="rename",
        dataset_id=dataset_id,
        actor=actor,
        path=new_path,
        detail=f"{action_note}: '{old_path}' → '{new_path}'",
    )
    return target


# --- tombstone ----------------------------------------------------------

def tombstone(
    inventory_path: Path,
    changelog_path: Path,
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
    rows = inventory_manager.load_inventory(inventory_path)
    target = _find_row(rows, dataset_id)

    if target.get("status") == "tombstoned":
        raise LifecycleError(f"dataset_id '{dataset_id}' is already tombstoned")

    file_path = str(target.get("file_path") or "")
    full_path = (library_root / file_path) if file_path else None

    if full_path and full_path.exists():
        full_path.unlink()
        action_note = "deleted file from library/"
    else:
        action_note = "file already absent from library/"

    target["status"] = "tombstoned"
    target["date_modified"] = utils.utc_now_date()
    inventory_manager.save_inventory(inventory_path, rows)

    detail = f"{action_note}; dataset_id retained for audit"
    if reason:
        detail = f"{detail}. reason: {reason}"

    inventory_manager.append_changelog(
        changelog_path,
        timestamp=utils.utc_now_iso(),
        action="remove",
        dataset_id=dataset_id,
        actor=actor,
        path=file_path or "—",
        detail=detail,
    )
    return target


# --- refresh ------------------------------------------------------------

# Snapshot fields recomputed from disk. Other locked columns
# (dataset_id, file_path, source_*, date_added) are not touched.
_REFRESH_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "checksum_sha256", "size_bytes", "mtime", "crs", "geographic_extent_bbox",
)


def refresh(
    inventory_path: Path,
    changelog_path: Path,
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
    accept drift that breaks format/CRS/naming compliance. If the
    in-place edit produced a non-canonical file, fix the file first
    (the inventory snapshot stays old in the meantime; reconcile will
    keep flagging the drift until refresh succeeds or the steward
    rolls back).

    No-op when nothing has changed.
    """
    rows = inventory_manager.load_inventory(inventory_path)
    target = _find_row(rows, dataset_id)

    if target.get("status") == "tombstoned":
        raise LifecycleError(
            f"dataset_id '{dataset_id}' is tombstoned; refresh not allowed"
        )

    file_path = str(target.get("file_path") or "")
    if not file_path:
        raise LifecycleError(f"dataset_id '{dataset_id}' has no file_path")

    full_path = library_root / file_path
    if not full_path.exists():
        raise LifecycleError(
            f"file '{file_path}' not found in library/ — run `y2y reconcile` "
            f"to investigate the ghost before attempting refresh."
        )

    # Canonical re-validation. Refusing drift that breaks compliance is
    # the whole point — refresh is "accept good drift," not "accept any drift."
    failures = validate_all(full_path)
    if failures:
        reasons = "; ".join(f"{check}: {reason}" for check, reason in failures)
        raise LifecycleError(
            f"file '{file_path}' fails canonical validators; refusing to "
            f"record bad state in the inventory. Fix the file first. "
            f"Failures: {reasons}"
        )

    # Recompute the snapshot from disk.
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

    # Diff against existing snapshot.
    diffs: list[tuple[str, Any, Any]] = []
    for field in _REFRESH_SNAPSHOT_FIELDS:
        old = target.get(field)
        new = new_snapshot[field]
        if old != new:
            diffs.append((field, old, new))

    if not diffs:
        return target  # no-op: nothing to record

    for field, _, new in diffs:
        target[field] = new
    target["date_modified"] = utils.utc_now_date()

    inventory_manager.save_inventory(inventory_path, rows)

    detail = " | ".join(f"{f}: {old!r} → {new!r}" for f, old, new in diffs)
    inventory_manager.append_changelog(
        changelog_path,
        timestamp=utils.utc_now_iso(),
        action="refresh",
        dataset_id=dataset_id,
        actor=actor,
        path=file_path,
        detail=f"snapshot refreshed from disk: {detail}",
    )
    return target
