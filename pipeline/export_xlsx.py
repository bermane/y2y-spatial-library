"""Generate ``inventory/inventory.xlsx`` from the SQLite catalogue.

The xlsx is a **regenerated, read-only artifact** — a steward-friendly
view of ``inventory.db``. Editing the xlsx changes nothing in the
catalogue. Re-export overwrites it.

Layout choices:

* Columns are grouped by header colour, in the same order as
  ``pending.xlsx``: steward-filled and published to ArcGIS Online,
  steward-filled and catalogue-only, pipeline-filled, then the
  columns the ArcGIS Online sync writes. Every schema column is
  included, so the export is a faithful render.
* Header colour-coding matches ``pending.xlsx`` (see
  ``pipeline/sheet_style.py``): who fills a column and whether it is
  published to ArcGIS Online. Purely informational here, since the
  whole sheet is read-only. A ``legend`` tab explains the colours.
* A second sheet ``changelog`` exposes the audit log so the whole
  catalogue is browsable in one workbook.
* ``inventory.xlsx`` is rejected if Excel has it open (lock file
  present); the steward must close it before re-exporting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import Workbook

from . import inventory_manager, sheet_style, steward_fields

INVENTORY_SHEET_NAME = "inventory"
CHANGELOG_SHEET_NAME = "changelog"

# (column_name, role). Roles drive the header colour and are defined in
# pipeline/sheet_style.py. The colours match pending.xlsx, so a column
# looks the same in both workbooks.
_INVENTORY_LAYOUT: list[tuple[str, str]] = [
    # blue: steward-filled, published to ArcGIS Online
    ("title", "agol_required"),
    ("summary", "agol_required"),
    ("description", "agol_required"),
    ("tags", "agol_required"),
    ("terms_of_use", "agol_required"),
    ("acknowledgements", "agol_required"),
    ("category", "agol_required"),
    ("subcategory", "agol_optional"),
    ("agol_format", "agol_required"),
    ("authoritative", "agol_required"),
    # orange: steward-filled, stays in the catalogue
    ("classification", "local_required"),
    ("status", "local_required"),
    ("data_steward", "local_required"),
    ("temporal_start", "local_optional"),
    ("temporal_end", "local_optional"),
    ("internal_notes", "local_optional"),
    # grey: pipeline-filled, in the same order as pending.xlsx: dates,
    # the file as it arrived (source_*), the file as stored in the
    # library (library_*), then the technical snapshot.
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
    ("dataset_type", "system"),
    ("checksum_sha256", "system"),
    ("size_bytes", "system"),
    ("mtime", "system"),
    ("geographic_extent_bbox", "system"),
    ("footprint_wkt", "system"),
    ("feature_count", "system"),
    ("raster_width", "system"),
    ("raster_height", "system"),
    ("pixel_size_x", "system"),
    ("pixel_size_y", "system"),
    # mauve: written by the ArcGIS Online sync
    ("agol_item_id", "agol_state"),
    ("agol_published_at", "agol_state"),
    ("last_synced_at", "agol_state"),
    ("sync_status", "agol_state"),
]
_INVENTORY_COLUMN_NAMES: list[str] = [c for c, _ in _INVENTORY_LAYOUT]

_CHANGELOG_LAYOUT: list[tuple[str, str]] = [
    ("id", "system"),
    ("timestamp", "system"),
    ("dataset_id", "system"),
    ("action", "system"),
    ("field_changed", "system"),
    ("old_value", "system"),
    ("new_value", "system"),
    ("note", "system"),
    ("actor", "system"),
]
_CHANGELOG_COLUMN_NAMES: list[str] = [c for c, _ in _CHANGELOG_LAYOUT]


def _safety_check(out_path: Path) -> None:
    """Refuse to write if Excel has the target xlsx open (lock-file race)."""
    if out_path.exists():
        inventory_manager.assert_not_locked(out_path)


def export(
    db_path: Path,
    out_path: Path,
) -> tuple[int, int]:
    """Write a fresh xlsx to ``out_path`` from the catalogue at ``db_path``.

    Returns ``(dataset_count, changelog_count)``.
    """
    _safety_check(out_path)

    inventory_rows = inventory_manager.load_inventory(db_path)
    changelog_rows = inventory_manager.load_changelog(db_path)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    # Default sheet → inventory.
    ws_inv = wb.active
    ws_inv.title = INVENTORY_SHEET_NAME
    _write_sheet(ws_inv, _INVENTORY_LAYOUT, _INVENTORY_COLUMN_NAMES, inventory_rows)

    ws_log = wb.create_sheet(CHANGELOG_SHEET_NAME)
    _write_sheet(ws_log, _CHANGELOG_LAYOUT, _CHANGELOG_COLUMN_NAMES, changelog_rows)

    sheet_style.write_legend_sheet(
        wb, [role for _, role in _INVENTORY_LAYOUT + _CHANGELOG_LAYOUT],
    )

    wb.save(out_path)
    return len(inventory_rows), len(changelog_rows)


def _display(name: str, value: Any) -> Any:
    """Render a catalogue value the way the steward reads it."""
    if name == "authoritative":
        return steward_fields.format_authoritative(value)
    return value


def _write_sheet(
    ws: Any,
    layout: list[tuple[str, str]],
    column_names: list[str],
    rows: list[dict[str, Any]],
) -> None:
    """Render header (with role-coloured fill) + body for one sheet."""
    for col_idx, (name, role) in enumerate(layout, start=1):
        sheet_style.style_header_cell(
            ws.cell(row=1, column=col_idx, value=name), role,
        )

    for row_idx, row in enumerate(rows, start=2):
        for col_idx, name in enumerate(column_names, start=1):
            ws.cell(row=row_idx, column=col_idx, value=_display(name, row.get(name)))

    for col_idx, name in enumerate(column_names, start=1):
        letter = ws.cell(row=1, column=col_idx).column_letter
        ws.column_dimensions[letter].width = max(12, min(40, len(name) + 4))

    ws.freeze_panes = "A2"
