"""Inventory manager: read/write the SQLite catalogue and append to changelog.

As of migration 001 (2026-04-29) the source of truth is
``inventory/inventory.db``, not the legacy ``inventory.xlsx``. The xlsx
is now a regenerated read-only artifact (see ``pipeline.export_xlsx``).

This module is intentionally a thin façade over stdlib ``sqlite3`` —
no ORM, no query builder, no hidden transactions. Callers pass a
``db_path`` (Path to ``inventory.db``) and get back plain ``dict`` rows
or commit plain ``dict`` rows. The schema is canonical (see
``pipeline/schema.sql``); this module makes no attempt to validate
field values beyond what SQLite + the STRICT/CHECK constraints already
enforce at INSERT/UPDATE time.

The public function names mirror the pre-migration API
(:func:`load_inventory`, :func:`save_inventory`, :func:`append_inventory`,
:func:`append_changelog`) so the upstream callers (lifecycle, ingest,
reconcile) only need their argument types swapped from xlsx-path to
db-path.

Excel-lock detection (``assert_not_locked``) and ``InventoryLockedError``
remain here because the xlsx export still has to fail-fast when Excel
has the rendered xlsx open. They no longer guard catalogue mutations.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import ulid

from . import db as _db

# Public filenames (used by the CLI and a couple of helpers in callers).
INVENTORY_FILENAME = "inventory.xlsx"        # legacy, now an export target
DB_FILENAME = "inventory.db"                 # source of truth
CHANGELOG_FILENAME = "changelog.md"          # legacy human-readable, not regenerated


# ----------------------------------------------------------------------------
# Schema-derived constants
# ----------------------------------------------------------------------------
# Order matches the column ordering in ``pipeline/schema.sql``. Used by
# load/save/append and (re-exported for) ``pipeline.export_xlsx``.
DATASETS_COLUMNS: tuple[str, ...] = (
    # identity & taxonomy
    "dataset_id", "dataset_type", "title", "category", "subcategory",
    "file_path", "format",
    # extrinsic metadata
    "summary", "description", "tags", "terms_of_use",
    "acknowledgements", "data_steward", "internal_notes",
    # lifecycle state
    "status", "date_added", "date_modified",
    # AGOL linkage
    "agol_item_id", "agol_published_at", "last_synced_at", "sync_status",
    # intrinsic snapshot
    "checksum_sha256", "size_bytes", "mtime", "crs",
    "geographic_extent_bbox", "classification",
    # spatial properties
    "footprint_wkt", "temporal_start", "temporal_end",
    "feature_count",
    "raster_width", "raster_height", "pixel_size_x", "pixel_size_y",
    # source provenance
    "source_format", "source_filename", "source_crs", "source_layer",
)

# Backwards-compat alias used by tests + pending_sheet historically.
INVENTORY_COLUMN_NAMES: list[str] = list(DATASETS_COLUMNS)

CHANGELOG_COLUMNS: tuple[str, ...] = (
    "id", "timestamp", "dataset_id", "action",
    "field_changed", "old_value", "new_value", "note", "actor",
)

# Action values permitted by the schema CHECK on changelog.action.
# Re-exported so callers can guard input without re-typing the list.
CHANGELOG_ACTIONS: frozenset[str] = frozenset({
    "add", "update", "rename", "remove", "refresh", "metadata",
    "reconcile-note", "migrated_from_xlsx", "id_format_migration",
})


class InventoryLockedError(RuntimeError):
    """Raised when an .xlsx pipeline-write target appears open in Excel.

    Pre-migration this guarded inventory.xlsx; post-migration it only
    matters for the rendered export and for pending.xlsx (the in-flight
    review sheet). The catalogue itself is SQLite and not subject to
    Excel locks.
    """


def assert_not_locked(path: Path) -> None:
    """Raise :class:`InventoryLockedError` if ``path`` (an .xlsx) is open in Excel.

    Detection: presence of a sibling ``~$<filename>`` lock file.
    Cheap to fix when wrong — just delete the stray lock file.
    """
    lock = path.parent / f"~${path.name}"
    if lock.exists():
        raise InventoryLockedError(
            f"{path.name} appears to be open in Excel ({lock.name} present in "
            f"{path.parent}). Close Excel before running this command — "
            f"otherwise your manual save can clobber the pipeline's. "
            f"If no Excel instance is actually open, delete the stray lock: "
            f"`rm '{lock}'`"
        )


# ----------------------------------------------------------------------------
# datasets — read API
# ----------------------------------------------------------------------------

def load_inventory(db_path: Path) -> list[dict[str, Any]]:
    """Return every dataset row as a list of dicts. Empty list if db missing.

    The legacy xlsx-era version returned an empty list when the file
    didn't exist; this preserves that semantics so callers (reconcile,
    tests) still cope with a fresh checkout.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    with _db.connect(db_path) as conn:
        cur = conn.execute(
            f"SELECT {', '.join(DATASETS_COLUMNS)} FROM datasets ORDER BY dataset_id"
        )
        return [dict(r) for r in cur.fetchall()]


def get_dataset(db_path: Path, dataset_id: str) -> dict[str, Any] | None:
    """Return one row by ``dataset_id``, or ``None`` if absent."""
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    with _db.connect(db_path) as conn:
        cur = conn.execute(
            f"SELECT {', '.join(DATASETS_COLUMNS)} FROM datasets "
            f"WHERE dataset_id = ?",
            (dataset_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


# ----------------------------------------------------------------------------
# datasets — write API
# ----------------------------------------------------------------------------

def _row_for_insert(row: dict[str, Any]) -> tuple[Any, ...]:
    """Pull the canonical column tuple out of a (possibly extra-keyed) dict.

    Missing keys → NULL. Unknown keys are silently dropped — callers
    sometimes hand us pending-sheet rows with extra columns.
    """
    return tuple(row.get(c) for c in DATASETS_COLUMNS)


def insert_dataset(db_path: Path, row: dict[str, Any]) -> None:
    """INSERT a single new dataset row.

    Caller is responsible for supplying every NOT NULL field. STRICT
    mode + the schema's CHECK constraints will reject malformed rows.
    """
    placeholders = ", ".join(["?"] * len(DATASETS_COLUMNS))
    cols_sql = ", ".join(DATASETS_COLUMNS)
    sql = f"INSERT INTO datasets ({cols_sql}) VALUES ({placeholders})"
    with _db.connect(db_path) as conn, conn:
        conn.execute(sql, _row_for_insert(row))


def update_dataset(
    db_path: Path,
    dataset_id: str,
    fields: dict[str, Any],
) -> None:
    """UPDATE one or more columns on a single dataset row.

    Raises :class:`KeyError` if ``dataset_id`` is absent (so callers
    don't silently no-op on a typo). Unknown keys in ``fields`` are
    rejected — better to fail than to silently misspell a column.
    """
    if not fields:
        return  # tolerate the no-op caller

    bad = set(fields) - set(DATASETS_COLUMNS)
    if bad:
        raise ValueError(
            f"unknown columns in update: {sorted(bad)}. "
            f"Schema columns: {DATASETS_COLUMNS}"
        )

    cols = list(fields.keys())
    set_sql = ", ".join(f"{c} = ?" for c in cols)
    sql = f"UPDATE datasets SET {set_sql} WHERE dataset_id = ?"

    with _db.connect(db_path) as conn, conn:
        cur = conn.execute(sql, (*[fields[c] for c in cols], dataset_id))
        if cur.rowcount == 0:
            raise KeyError(f"dataset_id {dataset_id!r} not found")


def append_inventory(db_path: Path, new_rows: list[dict[str, Any]]) -> None:
    """INSERT rows whose ``dataset_id`` doesn't already exist; skip dupes.

    Mirrors the xlsx-era semantics: a re-scan of an already-promoted
    bundle is harmlessly idempotent.
    """
    if not new_rows:
        return
    placeholders = ", ".join(["?"] * len(DATASETS_COLUMNS))
    cols_sql = ", ".join(DATASETS_COLUMNS)
    insert_sql = f"INSERT INTO datasets ({cols_sql}) VALUES ({placeholders})"
    with _db.connect(db_path) as conn, conn:
        existing = {
            r[0] for r in conn.execute(
                "SELECT dataset_id FROM datasets"
            ).fetchall()
        }
        for row in new_rows:
            did = row.get("dataset_id")
            if not did or did in existing:
                continue
            conn.execute(insert_sql, _row_for_insert(row))
            existing.add(did)


def save_inventory(db_path: Path, rows: list[dict[str, Any]]) -> None:
    """UPSERT every supplied row by ``dataset_id``.

    Provided for the legacy ``load → mutate → save`` callers. Note this
    does **not** delete rows that are absent from ``rows`` — a true
    "replace all" would risk losing audit history (datasets are
    foreign-keyed by changelog rows). Callers wanting to remove a row
    should use :func:`update_dataset` to set status='tombstoned'.
    """
    if not rows:
        return
    placeholders = ", ".join(["?"] * len(DATASETS_COLUMNS))
    cols_sql = ", ".join(DATASETS_COLUMNS)
    update_set_sql = ", ".join(
        f"{c} = excluded.{c}" for c in DATASETS_COLUMNS if c != "dataset_id"
    )
    sql = (
        f"INSERT INTO datasets ({cols_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT(dataset_id) DO UPDATE SET {update_set_sql}"
    )
    with _db.connect(db_path) as conn, conn:
        for row in rows:
            conn.execute(sql, _row_for_insert(row))


# ----------------------------------------------------------------------------
# changelog
# ----------------------------------------------------------------------------

def append_changelog(
    db_path: Path,
    *,
    timestamp: str,
    action: str,
    dataset_id: str,
    actor: str,
    path: str | None,
    detail: str,
    field_changed: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
) -> None:
    """Insert one changelog row.

    The pre-migration API took ``path`` as a structural argument; the
    SQLite changelog table has no path column (file_path lives on the
    dataset row, joinable by dataset_id). For backwards compatibility
    ``path`` is folded into ``note`` as a ``[path: …]`` prefix when
    supplied.

    ``field_changed`` / ``old_value`` / ``new_value`` are new — they
    let callers record structured per-field diffs. Existing call sites
    pass diffs as free text in ``detail``; that still works.
    """
    if action not in CHANGELOG_ACTIONS:
        raise ValueError(
            f"changelog action {action!r} not allowed. "
            f"Permitted: {sorted(CHANGELOG_ACTIONS)}"
        )

    note = detail
    if path and path != "—":
        note = f"[path: {path}] {detail}"

    cl_id = f"cl_{ulid.ULID()}"
    sql = (
        "INSERT INTO changelog "
        "(id, timestamp, dataset_id, action, field_changed, "
        " old_value, new_value, note, actor) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    with _db.connect(db_path) as conn, conn:
        try:
            conn.execute(
                sql,
                (cl_id, timestamp, dataset_id, action,
                 field_changed, old_value, new_value, note, actor),
            )
        except sqlite3.IntegrityError as exc:
            # Most likely cause: dataset_id doesn't exist (FK RESTRICT).
            raise ValueError(
                f"failed to append changelog for dataset_id {dataset_id!r}: {exc}"
            ) from exc


def load_changelog(db_path: Path) -> list[dict[str, Any]]:
    """Return every changelog row, oldest first. For audit / export."""
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    with _db.connect(db_path) as conn:
        cur = conn.execute(
            f"SELECT {', '.join(CHANGELOG_COLUMNS)} FROM changelog "
            f"ORDER BY timestamp, id"
        )
        return [dict(r) for r in cur.fetchall()]
