"""Management of queue/processing/pending.xlsx — the Phase-1 review sheet.

The sheet is persistent across scans (DESIGN.md §8). On each scan the
pipeline appends rows for newly-staged datasets; on approve it removes
rows that have been promoted. Rows are identified by ``dataset_id``.

Column layout: ``ready`` and ``_validation_error`` first, then the
columns grouped by header colour: steward-filled and published to
ArcGIS Online, steward-filled and catalogue-only, pipeline-filled.

Header colours come from :mod:`pipeline.sheet_style` and are explained
in the workbook's ``legend`` tab: blue columns are steward-filled and
published to ArcGIS Online, orange columns are steward-filled and stay
in the catalogue, grey and mauve columns are never edited by hand.

See DESIGN.md §7 for the full inventory schema and §8 for the two-phase
ingestion workflow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook

from . import sheet_style
from .inventory_manager import assert_not_locked

PENDING_FILENAME = "pending.xlsx"
PENDING_SHEET_NAME = "pending"

READY_COLUMN = "ready"
ERROR_COLUMN = "_validation_error"

# Pipeline-only column that lives in pending.xlsx but never makes it
# into inventory.xlsx. ``target_filename`` is the transformation input
# the steward confirms (or overrides) before approval.
TARGET_FILENAME_COLUMN = "target_filename"


# Columns are grouped by header colour, in the order the steward works
# through a row (steward preference 2026-11-24):
#
#   1. control + error sink, so the row's promote-state is visible first
#   2. blue: steward-filled, published to ArcGIS Online
#   3. orange: steward-filled, stays in the catalogue
#   4. grey: pipeline-filled; nothing to do here during review
#   5. mauve: written by the ArcGIS Online sync
#
# Within blue and orange the optional (lighter) columns sit next to the
# column they belong with. inventory.xlsx follows the same grouping.
COLUMNS: list[tuple[str, str]] = [
    # 1. control + error
    (READY_COLUMN, "control"),
    (ERROR_COLUMN, "error"),
    # 2. blue: steward-filled, published to ArcGIS Online
    ("title", "agol_required"),
    ("summary", "agol_required"),
    ("description", "agol_required"),
    # ``tags`` is required, but ``NA`` marks "no tags on purpose".
    # See pipeline/steward_fields.py.
    ("tags", "agol_required"),
    ("terms_of_use", "agol_required"),
    ("acknowledgements", "agol_required"),
    # Auto-prefilled where possible; the steward confirms or corrects.
    ("category", "agol_required"),
    ("subcategory", "agol_optional"),
    # Auto-prefilled from format: ``feature-layer`` for vectors,
    # ``imagery-layer`` for rasters. Steward overrides to
    # ``vector-tile-layer`` for large vectors that should be delivered
    # as cached tiles. ingest.approve() rejects empty/invalid values.
    # See DESIGN.md §15.
    ("agol_format", "agol_required"),
    # Pre-filled ``yes``; the steward sets ``no`` for the exceptions.
    ("authoritative", "agol_required"),
    # 3. orange: steward-filled, stays in the catalogue
    (TARGET_FILENAME_COLUMN, "local_required"),
    ("classification", "local_required"),
    ("status", "local_required"),
    ("data_steward", "local_required"),
    ("temporal_start", "local_optional"),
    ("temporal_end", "local_optional"),
    ("notes", "local_optional"),
    # 4. grey: pipeline-filled (steward-chosen order, 2026-11-24):
    #    dates, then the file as it arrived (source_*), then the file
    #    as stored in the library (library_*), then the technical
    #    snapshot. The source_* and date_added columns are filled at
    #    scan; the rest at approve.
    ("date_added", "system"),
    ("date_modified", "system"),
    ("source_filename", "system"),
    ("source_format", "system"),
    ("source_crs", "system"),
    ("source_layer", "system"),
    ("file_path", "system"),
    ("library_format", "system"),
    ("library_crs", "system"),
    ("dataset_id", "system"),
    ("checksum_sha256", "system"),
    ("size_bytes", "system"),
    ("mtime", "system"),
    ("geographic_extent_bbox", "system"),
    # 5. mauve: written by the first AGOL push. Never filled in by the
    #    steward; ingest.approve() rejects a row where it has been typed in.
    ("agol_item_id", "agol_state"),
]
COLUMN_NAMES: list[str] = [name for name, _ in COLUMNS]


def load_pending(path: Path) -> list[dict[str, Any]]:
    """Return rows as list-of-dicts. Empty list if the file doesn't exist."""
    if not path.exists():
        return []
    wb = load_workbook(path)
    ws = wb[PENDING_SHEET_NAME]
    headers = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
    rows: list[dict[str, Any]] = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if all(v is None for v in row):
            continue
        rows.append(dict(zip(headers, row)))
    return rows


def save_pending(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write ``rows`` to ``path``, replacing any existing content.

    Refuses to write if Excel has the file open (race-prevention).
    """
    assert_not_locked(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = PENDING_SHEET_NAME

    for col_idx, (name, role) in enumerate(COLUMNS, start=1):
        sheet_style.style_header_cell(
            ws.cell(row=1, column=col_idx, value=name), role,
        )

    for row_idx, row in enumerate(rows, start=2):
        for col_idx, name in enumerate(COLUMN_NAMES, start=1):
            ws.cell(row=row_idx, column=col_idx, value=row.get(name))

    for col_idx, name in enumerate(COLUMN_NAMES, start=1):
        letter = ws.cell(row=1, column=col_idx).column_letter
        ws.column_dimensions[letter].width = max(12, min(40, len(name) + 4))

    ws.freeze_panes = "A2"
    sheet_style.write_legend_sheet(wb, [role for _, role in COLUMNS])
    wb.save(path)


def append_pending(path: Path, new_rows: list[dict[str, Any]]) -> None:
    """Load the existing sheet (if any), append new rows, save.

    Rows whose ``dataset_id`` already appears in the sheet are skipped,
    so re-running scan on the same file is idempotent.
    """
    existing = load_pending(path)
    seen = {r.get("dataset_id") for r in existing if r.get("dataset_id")}
    merged = list(existing)
    for row in new_rows:
        if row.get("dataset_id") in seen:
            continue
        merged.append(row)
    save_pending(path, merged)


def delete_if_empty(path: Path) -> bool:
    """Delete pending.xlsx if it has no data rows. Returns True if deleted."""
    if not path.exists():
        return False
    rows = load_pending(path)
    if not rows:
        path.unlink()
        return True
    return False
