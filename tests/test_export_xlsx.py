"""Tests for the read-only inventory.xlsx export."""

from __future__ import annotations

from openpyxl import load_workbook

from pipeline import export_xlsx, inventory_manager, lifecycle, sheet_style


def test_export_colours_legend_and_authoritative(project_tree, populate_dataset) -> None:
    yes_id, _ = populate_dataset(filename="streams_a.gpkg")
    no_id, _ = populate_dataset(filename="streams_b.gpkg")
    lifecycle.update(
        project_tree["db"], dataset_id=no_id,
        fields={"authoritative": "no", "tags": "NA"}, actor="t",
    )

    out = project_tree["root"] / "inventory" / "inventory.xlsx"
    n_datasets, _ = export_xlsx.export(project_tree["db"], out)
    assert n_datasets == 2

    wb = load_workbook(out)
    assert wb.sheetnames == ["inventory", "changelog", "legend"]

    ws = wb["inventory"]
    headers = [c.value for c in ws[1]]
    # Every catalogue column is rendered.
    assert set(headers) == set(inventory_manager.DATASETS_COLUMNS)

    colour = {c.value: c.fill.start_color.rgb[-6:] for c in ws[1]}
    hex_of = {role: v[0] for role, v in sheet_style.ROLES.items()}
    assert colour["title"] == colour["tags"] == colour["authoritative"] == hex_of["agol_required"]
    assert colour["internal_notes"] == colour["temporal_end"] == hex_of["local_optional"]
    assert colour["dataset_id"] == colour["checksum_sha256"] == hex_of["system"]
    assert colour["agol_item_id"] == colour["sync_status"] == hex_of["agol_state"]
    assert colour["agol_item_id"] != colour["internal_notes"]

    rows = {
        r[headers.index("dataset_id")]: dict(zip(headers, r))
        for r in ws.iter_rows(min_row=2, values_only=True)
    }
    assert rows[yes_id]["authoritative"] == "yes"
    assert rows[no_id]["authoritative"] == "no"
    assert rows[no_id]["tags"] == "NA"
