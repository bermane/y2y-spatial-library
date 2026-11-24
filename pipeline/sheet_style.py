"""Header colours shared by ``pending.xlsx`` and ``inventory.xlsx``.

Both workbooks colour each column header by its role, so the steward can
see at a glance who fills a column and whether its value is published to
ArcGIS Online. A ``legend`` tab in each workbook spells the colours out.

Roles:

    control        ``ready``: the steward's promote switch
    agol_required  steward fills; published to ArcGIS Online; required
    agol_optional  steward fills; published to ArcGIS Online; optional
    local_required steward fills; stays in the catalogue; required
    local_optional steward fills; stays in the catalogue; optional
    system         filled by the pipeline; never edit
    agol_state     written by the ArcGIS Online sync; never edit
    error          pipeline-written error message

Required and optional share a hue; required is the darker shade.
"""

from __future__ import annotations

from typing import Any

from openpyxl.styles import Alignment, Font, PatternFill

LEGEND_SHEET_NAME = "legend"

# role -> (hex fill, label, meaning). Order is the legend's row order.
ROLES: dict[str, tuple[str, str, str]] = {
    "control": (
        "FFE066", "Yellow",
        "Control. Set ready to TRUE when the row is complete.",
    ),
    "agol_required": (
        "9FC5E8", "Blue",
        "You fill this. It is published to ArcGIS Online. Required.",
    ),
    "agol_optional": (
        "DDEBF7", "Light blue",
        "You fill this. It is published to ArcGIS Online. Optional.",
    ),
    "local_required": (
        "F9CB9C", "Orange",
        "You fill this. It stays in the catalogue. Required.",
    ),
    "local_optional": (
        "FDEBD9", "Light orange",
        "You fill this. It stays in the catalogue. Optional.",
    ),
    "system": (
        "D9D9D9", "Grey",
        "Filled by the pipeline. Do not edit.",
    ),
    "agol_state": (
        "D5A6BD", "Mauve",
        "Written by the ArcGIS Online sync. Do not edit.",
    ),
    "error": (
        "CC0000", "Red",
        "Error message from the pipeline. Fix the row, then set ready to TRUE again.",
    ),
}

FILLS: dict[str, PatternFill] = {
    role: PatternFill("solid", start_color=hex_) for role, (hex_, _, _) in ROLES.items()
}

_FONT_DARK = Font(bold=True, color="000000")
_FONT_LIGHT = Font(bold=True, color="FFFFFF")

# Extra guidance shown under the colour key in the legend tab.
FIELD_NOTES: dict[str, str] = {
    "tags": (
        "Separate tags with a semicolon, for example: grizzly bear; habitat; "
        "Alberta. Enter NA if the dataset should have no tags. The cell "
        "cannot be left empty."
    ),
    "authoritative": (
        "yes or no. Pre-filled yes, because the library holds authoritative "
        "datasets. Change it to no for the exceptions."
    ),
    "temporal_start / temporal_end": (
        "The period the data covers. Use YYYY, YYYY-MM or YYYY-MM-DD. "
        "Leave empty if it does not apply."
    ),
    "agol_item_id": (
        "Filled in automatically when the dataset is published to ArcGIS "
        "Online."
    ),
}


def style_header_cell(cell: Any, role: str) -> None:
    """Apply the role's fill, a bold font and centring to a header cell."""
    cell.fill = FILLS[role]
    cell.font = _FONT_LIGHT if role == "error" else _FONT_DARK
    cell.alignment = Alignment(horizontal="center", vertical="center")


def write_legend_sheet(wb: Any, roles: list[str]) -> None:
    """Add a ``legend`` tab explaining the header colours used in ``wb``.

    ``roles`` lists the roles that appear in the workbook; the legend
    shows them in the canonical order of :data:`ROLES`.
    """
    ws = wb.create_sheet(LEGEND_SHEET_NAME)
    ws.cell(row=1, column=1, value="Header colour").font = _FONT_DARK
    ws.cell(row=1, column=2, value="Meaning").font = _FONT_DARK

    row_idx = 2
    for role, (_, label, meaning) in ROLES.items():
        if role not in roles:
            continue
        style_header_cell(ws.cell(row=row_idx, column=1, value=label), role)
        ws.cell(row=row_idx, column=2, value=meaning)
        row_idx += 1

    row_idx += 1
    ws.cell(row=row_idx, column=1, value="Column").font = _FONT_DARK
    ws.cell(row=row_idx, column=2, value="How to fill it").font = _FONT_DARK
    for name, note in FIELD_NOTES.items():
        row_idx += 1
        ws.cell(row=row_idx, column=1, value=name)
        ws.cell(row=row_idx, column=2, value=note)

    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 110
