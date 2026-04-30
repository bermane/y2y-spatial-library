"""End-to-end Phase 3 (approve) tests with real GeoPackage fixtures.

In the three-phase model, approve runs the transformation, then strict
canonical validation, then snapshot, then promote, then archive source.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import geopandas as gpd
from shapely.geometry import Point

from pipeline import ingest, inventory_manager, pending_sheet


def _fill_required(row: dict[str, Any], **overrides: Any) -> None:
    row["ready"] = True
    row["category"] = "Water"  # display name == folder name for Water
    row["data_steward"] = "Ethan Berman"
    row["title"] = "Streams 2024 (Test)"
    row["summary"] = "A hypothetical streams dataset for testing."
    row["description"] = "Long-form description placeholder."
    row["tags"] = "streams;test;y2y"
    row["terms_of_use"] = "Internal use only; do not redistribute."
    row["acknowledgements"] = "Test fixture; not real data."
    row.update(overrides)


def _scan_then_load_row(
    project_tree, valid_gpkg_factory, filename: str = "streams_2024.gpkg",
):
    valid_gpkg_factory(filename, dest_dir=project_tree["incoming"])
    ingest.scan(project_tree["incoming"], project_tree["processing"], project_tree["rejected"])
    pending_path = project_tree["processing"] / pending_sheet.PENDING_FILENAME
    rows = pending_sheet.load_pending(pending_path)
    assert len(rows) == 1
    return pending_path, rows[0]


def _approve(project_tree):
    return ingest.approve(
        project_tree["processing"], project_tree["library"],
        project_tree["db"],
        actor="tester",
    )


# --- happy paths --------------------------------------------------------

def test_approve_promotes_a_complete_ready_row(project_tree, valid_gpkg_factory) -> None:
    pending_path, row = _scan_then_load_row(project_tree, valid_gpkg_factory)
    _fill_required(row)
    pending_sheet.save_pending(pending_path, [row])

    result = _approve(project_tree)

    assert result.promoted == 1
    assert result.failed == 0
    assert result.pending_deleted is True

    assert (project_tree["library"] / "Water" / "streams_2024.gpkg").exists()
    # Per-dataset staging dir was archived (not left in processing)
    assert not (project_tree["processing"] / row["dataset_id"]).exists()

    # Source is archived to queue/archived/<dataset_id>/
    archived_root = project_tree["root"] / "queue" / "archived" / row["dataset_id"]
    assert archived_root.is_dir()
    assert (archived_root / "streams_2024.gpkg").exists()
    # The transient _canonical/ output dir is not archived
    assert not (archived_root / "_canonical").exists()

    inv_rows = inventory_manager.load_inventory(project_tree["db"])
    assert len(inv_rows) == 1
    promoted = inv_rows[0]
    assert promoted["file_path"] == "Water/streams_2024.gpkg"
    # Schema CHECKs are lowercase enum values; the pending sheet's
    # display-cased values get mapped at insert.
    assert promoted["format"] == "geopackage"
    assert promoted["source_format"] == "geopackage"
    assert promoted["source_filename"] == "streams_2024.gpkg"
    assert promoted["crs"] == "ESRI:102008"
    assert promoted["checksum_sha256"]
    assert promoted["dataset_type"] == "spatial"
    assert promoted["sync_status"] == "unpublished"
    # Spatial-properties columns backfilled from the canonical file.
    assert promoted["feature_count"] == 1
    assert promoted["footprint_wkt"] is not None
    # Pending-only fields don't make it into datasets.
    assert "ready" not in promoted
    assert "target_filename" not in promoted

    log = inventory_manager.load_changelog(project_tree["db"])
    add_entries = [r for r in log if r["action"] == "add" and r["dataset_id"] == row["dataset_id"]]
    assert len(add_entries) == 1


def test_approve_promotes_species_with_subcategory(project_tree, valid_gpkg_factory) -> None:
    pending_path, row = _scan_then_load_row(
        project_tree, valid_gpkg_factory, filename="grizzly_den_sites_2024.gpkg",
    )
    _fill_required(row, category="Species & Species at Risk", subcategory="Grizzly Bear")
    pending_sheet.save_pending(pending_path, [row])

    assert _approve(project_tree).promoted == 1
    assert (
        project_tree["library"] / "Species" / "Grizzly_Bear" / "grizzly_den_sites_2024.gpkg"
    ).exists()


def test_approve_converts_shapefile_to_gpkg(project_tree) -> None:
    """Phase 3 converts source format → canonical and reprojects to ESRI:102008."""
    shp = project_tree["incoming"] / "roads_2024.shp"
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(-120.0, 50.0)], crs="EPSG:4326")
    gdf.to_file(shp, driver="ESRI Shapefile")

    ingest.scan(project_tree["incoming"], project_tree["processing"], project_tree["rejected"])
    pending_path = project_tree["processing"] / pending_sheet.PENDING_FILENAME
    rows = pending_sheet.load_pending(pending_path)
    row = rows[0]
    _fill_required(row, category="Connectivity & Wildlife Movement")
    pending_sheet.save_pending(pending_path, [row])

    result = _approve(project_tree)

    assert result.promoted == 1
    library_target = (
        project_tree["library"] / "Connectivity_Wildlife_Movement" / "roads_2024.gpkg"
    )
    assert library_target.exists()
    inv = inventory_manager.load_inventory(project_tree["db"])
    assert inv[0]["source_format"] == "shapefile"
    assert inv[0]["source_crs"] == "EPSG:4326"
    assert inv[0]["crs"] == "ESRI:102008"  # reprojected
    assert inv[0]["format"] == "geopackage"
    # Inventory carries the display name, not the folder name
    assert inv[0]["category"] == "Connectivity & Wildlife Movement"


# --- skip & validation rejections --------------------------------------

def test_approve_skips_rows_that_are_not_ready(project_tree, valid_gpkg_factory) -> None:
    pending_path, row = _scan_then_load_row(project_tree, valid_gpkg_factory)
    _fill_required(row)
    row["ready"] = False
    pending_sheet.save_pending(pending_path, [row])

    result = _approve(project_tree)

    assert result.promoted == 0
    assert result.skipped == 1
    # Source still in its per-dataset staging dir
    assert (project_tree["processing"] / row["dataset_id"] / "streams_2024.gpkg").exists()


def test_approve_fails_when_required_field_missing(project_tree, valid_gpkg_factory) -> None:
    pending_path, row = _scan_then_load_row(project_tree, valid_gpkg_factory)
    _fill_required(row)
    row["acknowledgements"] = None
    pending_sheet.save_pending(pending_path, [row])

    result = _approve(project_tree)

    assert result.failed == 1
    rows_after = pending_sheet.load_pending(pending_path)
    err = rows_after[0]["_validation_error"] or ""
    assert "acknowledgements" in err


def test_approve_fails_on_invalid_category(project_tree, valid_gpkg_factory) -> None:
    pending_path, row = _scan_then_load_row(project_tree, valid_gpkg_factory)
    _fill_required(row, category="Made_Up_Category")
    pending_sheet.save_pending(pending_path, [row])

    assert _approve(project_tree).failed == 1
    err = pending_sheet.load_pending(pending_path)[0]["_validation_error"]
    assert "category" in err


def test_approve_fails_species_without_subcategory(project_tree, valid_gpkg_factory) -> None:
    pending_path, row = _scan_then_load_row(project_tree, valid_gpkg_factory)
    _fill_required(row, category="Species & Species at Risk", subcategory=None)
    pending_sheet.save_pending(pending_path, [row])

    assert _approve(project_tree).failed == 1
    err = pending_sheet.load_pending(pending_path)[0]["_validation_error"]
    assert "subcategory" in err


def test_approve_fails_subcategory_set_for_non_species(project_tree, valid_gpkg_factory) -> None:
    pending_path, row = _scan_then_load_row(project_tree, valid_gpkg_factory)
    _fill_required(row, category="Water", subcategory="Streams")
    pending_sheet.save_pending(pending_path, [row])

    assert _approve(project_tree).failed == 1


def test_approve_fails_target_filename_naming_violation(project_tree, valid_gpkg_factory) -> None:
    """Steward overrode target_filename with an uppercase name → naming validator catches it."""
    pending_path, row = _scan_then_load_row(project_tree, valid_gpkg_factory)
    _fill_required(row)
    row["target_filename"] = "Streams.gpkg"  # uppercase: invalid
    pending_sheet.save_pending(pending_path, [row])

    assert _approve(project_tree).failed == 1
    err = pending_sheet.load_pending(pending_path)[0]["_validation_error"]
    assert "target_filename" in err


def test_approve_detects_missing_source_file(project_tree, valid_gpkg_factory) -> None:
    """Steward edited source_filename to point at something that's not in processing/."""
    pending_path, row = _scan_then_load_row(project_tree, valid_gpkg_factory)
    _fill_required(row)
    row["source_filename"] = "nonexistent.gpkg"
    pending_sheet.save_pending(pending_path, [row])

    assert _approve(project_tree).failed == 1
    err = pending_sheet.load_pending(pending_path)[0]["_validation_error"]
    assert "not found" in err


def test_approve_with_no_pending_sheet_is_a_noop(project_tree) -> None:
    result = _approve(project_tree)
    assert (result.promoted, result.failed, result.skipped) == (0, 0, 0)
