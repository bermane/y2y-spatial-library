"""End-to-end Phase 1 (scan) tests under the lenient three-phase model.

Scan accepts any single-layer / single-band source from the allow-list
(Shapefile, GPKG, GeoJSON, KML/KMZ, single-band GeoTIFF). Format/CRS/
naming validation now runs at approval time on the *transformed* file —
not at scan. The only scan-time rejections are: multi-layer/multi-band
sources, sources that won't open at all, and staging conflicts.
"""

from __future__ import annotations

from pathlib import Path

import fiona
import geopandas as gpd
from fiona.crs import CRS as FionaCRS
from shapely.geometry import Point

from pipeline import ingest, pending_sheet


# --- happy path --------------------------------------------------------

def test_scan_accepts_valid_gpkg(project_tree, valid_gpkg_factory) -> None:
    valid_gpkg_factory("streams_2024.gpkg", dest_dir=project_tree["incoming"])

    result = ingest.scan(project_tree["incoming"], project_tree["processing"], project_tree["rejected"])

    assert result.accepted == 1
    assert result.rejected == 0
    assert not (project_tree["incoming"] / "streams_2024.gpkg").exists()
    # Source is staged under a per-dataset_id subdirectory.
    rows = pending_sheet.load_pending(result.pending_path)
    assert (project_tree["processing"] / rows[0]["dataset_id"] / "streams_2024.gpkg").exists()
    assert result.pending_path.exists()


def test_scan_accepts_uppercase_filename_now(project_tree, valid_gpkg_factory) -> None:
    """Filename convention is checked against `target_filename`, not source.
    Auto-proposed target_filename is slugified, so an uppercase source is fine."""
    valid_gpkg_factory("Streams_2024.gpkg", dest_dir=project_tree["incoming"])

    result = ingest.scan(project_tree["incoming"], project_tree["processing"], project_tree["rejected"])

    assert result.accepted == 1
    assert result.rejected == 0
    rows = pending_sheet.load_pending(result.pending_path)
    assert rows[0]["source_filename"] == "Streams_2024.gpkg"
    assert rows[0]["target_filename"] == "streams_2024.gpkg"


# --- pending row shape -------------------------------------------------

def test_scan_pending_row_captures_source_metadata(project_tree, valid_gpkg_factory) -> None:
    valid_gpkg_factory("streams_2024.gpkg", dest_dir=project_tree["incoming"])
    ingest.scan(project_tree["incoming"], project_tree["processing"], project_tree["rejected"])

    rows = pending_sheet.load_pending(project_tree["processing"] / pending_sheet.PENDING_FILENAME)
    assert len(rows) == 1
    row = rows[0]

    # Always populated at scan
    assert row["dataset_id"].startswith("ds_")
    assert row["status"] == "active"
    assert row["ready"] is False
    # Title is required-empty (steward fills it; not auto-derived from filename)
    assert row["title"] is None

    # Source provenance — captured from the incoming file
    assert row["source_format"] == "GeoPackage"
    assert row["source_filename"] == "streams_2024.gpkg"
    assert row["source_crs"] == "ESRI:102008"
    assert row["source_layer"] is None  # Phase A: single-layer sources

    # Target proposal — auto-derived from source stem
    assert row["target_filename"] == "streams_2024.gpkg"
    assert row["format"] == "GeoPackage"

    # Intrinsic snapshot — empty until approve
    assert row["file_path"] is None
    assert row["checksum_sha256"] is None
    assert row["size_bytes"] is None
    assert row["mtime"] is None
    assert row["crs"] is None
    assert row["geographic_extent_bbox"] is None

    # Category was auto-prefilled from filename keywords ("streams" → Water).
    # Steward can override; this test asserts the prefill happened.
    assert row["category"] == "Water"
    assert row["subcategory"] is None
    # Required extrinsic fields still empty for the steward
    assert row["summary"] is None


def test_scan_proposes_canonical_target_for_shapefile(project_tree) -> None:
    """A Shapefile → target_filename ends in .gpkg (transformer will convert)."""
    shp = project_tree["incoming"] / "roads_2024.shp"
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326")
    gdf.to_file(shp, driver="ESRI Shapefile")

    result = ingest.scan(project_tree["incoming"], project_tree["processing"], project_tree["rejected"])

    assert result.accepted == 1
    rows = pending_sheet.load_pending(result.pending_path)
    row = rows[0]
    assert row["source_format"] == "Shapefile"
    assert row["source_crs"] == "EPSG:4326"
    assert row["target_filename"] == "roads_2024.gpkg"
    assert row["format"] == "GeoPackage"


def test_scan_moves_shapefile_bundle(project_tree) -> None:
    """All Shapefile sidecars travel together into processing/<dataset_id>/."""
    shp = project_tree["incoming"] / "roads.shp"
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326")
    gdf.to_file(shp, driver="ESRI Shapefile")

    sidecars_before = sorted(p.suffix for p in project_tree["incoming"].iterdir())
    assert ".shp" in sidecars_before and ".dbf" in sidecars_before

    result = ingest.scan(project_tree["incoming"], project_tree["processing"], project_tree["rejected"])

    assert not any(project_tree["incoming"].iterdir())
    rows = pending_sheet.load_pending(result.pending_path)
    staging = project_tree["processing"] / rows[0]["dataset_id"]
    moved = sorted(p.suffix for p in staging.iterdir())
    for required in (".shp", ".shx", ".dbf"):
        assert required in moved


# --- multi-layer rejection (Phase A) -----------------------------------

def test_scan_rejects_multi_layer_gpkg(project_tree, tmp_path: Path) -> None:
    """A GPKG with two layers is rejected with an 'extract layers first' message."""
    multi = project_tree["incoming"] / "multi.gpkg"
    gdf1 = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326")
    gdf2 = gpd.GeoDataFrame({"id": [2]}, geometry=[Point(1, 1)], crs="EPSG:4326")
    gdf1.to_file(multi, driver="GPKG", layer="layer_a")
    gdf2.to_file(multi, driver="GPKG", layer="layer_b")

    result = ingest.scan(project_tree["incoming"], project_tree["processing"], project_tree["rejected"])

    assert result.accepted == 0
    assert result.rejected == 1
    sidecars = list(project_tree["rejected"].glob("*.rejected.yaml"))
    assert len(sidecars) == 1
    text = sidecars[0].read_text()
    assert "single-layer" in text or "layers" in text


def test_scan_rejects_multi_band_tiff(project_tree, valid_cog_factory) -> None:
    """A multi-band TIFF is rejected at scan."""
    import rasterio
    import numpy as np
    from rasterio.enums import Resampling

    path = project_tree["incoming"] / "rgb.tif"
    profile = {
        "driver": "GTiff", "dtype": "uint8", "count": 3,
        "width": 1024, "height": 1024, "crs": "ESRI:102008",
        "transform": rasterio.transform.from_origin(0, 0, 100, 100),
        "tiled": True, "blockxsize": 512, "blockysize": 512,
        "compress": "zstd", "predictor": 2, "zstd_level": 9,
        "nodata": 255, "BIGTIFF": "IF_NEEDED",
    }
    with rasterio.open(path, "w", **profile) as dst:
        for b in range(1, 4):
            dst.write(np.zeros((1024, 1024), dtype="uint8"), b)
        dst.build_overviews([2], Resampling.nearest)

    result = ingest.scan(project_tree["incoming"], project_tree["processing"], project_tree["rejected"])

    assert result.rejected == 1
    sidecars = list(project_tree["rejected"].glob("*.rejected.yaml"))
    assert "single-band" in sidecars[0].read_text()


# --- idempotency, hidden-file skip --------------------------------------

def test_scan_is_idempotent_across_runs(project_tree, valid_gpkg_factory) -> None:
    valid_gpkg_factory("streams_2024.gpkg", dest_dir=project_tree["incoming"])
    ingest.scan(project_tree["incoming"], project_tree["processing"], project_tree["rejected"])

    valid_gpkg_factory("rivers_2024.gpkg", dest_dir=project_tree["incoming"])
    result = ingest.scan(project_tree["incoming"], project_tree["processing"], project_tree["rejected"])

    assert result.accepted == 1
    rows = pending_sheet.load_pending(result.pending_path)
    sources = {r["source_filename"] for r in rows}
    assert sources == {"streams_2024.gpkg", "rivers_2024.gpkg"}


def test_scan_skips_hidden_and_dirs(project_tree) -> None:
    (project_tree["incoming"] / ".DS_Store").write_bytes(b"junk")
    (project_tree["incoming"] / "some_subdir").mkdir()

    result = ingest.scan(project_tree["incoming"], project_tree["processing"], project_tree["rejected"])

    assert result.accepted == 0
    assert result.rejected == 0


def test_scan_skips_unrecognized_extensions(project_tree) -> None:
    (project_tree["incoming"] / "notes.txt").write_text("not spatial")
    (project_tree["incoming"] / "report.pdf").write_bytes(b"%PDF-1.4")

    result = ingest.scan(project_tree["incoming"], project_tree["processing"], project_tree["rejected"])

    assert result.accepted == 0
    assert result.rejected == 0
    # The files stay where they were — we don't rename or remove them
    assert (project_tree["incoming"] / "notes.txt").exists()
    assert (project_tree["incoming"] / "report.pdf").exists()


def test_scan_pairs_gpkg_with_matching_vtpk(project_tree, valid_gpkg_factory) -> None:
    """When a .gpkg and a same-stem .vtpk are both in queue/incoming/,
    scan moves both into queue/processing/<dataset_id>/ as a paired
    bundle and pre-fills agol_format='vector-tile-layer' in the
    pending row (instead of the default feature-layer for vectors).
    """
    from pipeline import pending_sheet

    valid_gpkg_factory("parks.gpkg", dest_dir=project_tree["incoming"])
    (project_tree["incoming"] / "parks.vtpk").write_bytes(b"PK\x03\x04fake-vtpk")

    result = ingest.scan(
        project_tree["incoming"],
        project_tree["processing"],
        project_tree["rejected"],
    )
    assert result.accepted == 1
    # The VTPK did NOT generate an unmatched ingest result — it was
    # paired with the GPKG instead.
    assert all(r.status != "unmatched" for r in result.vtpk_results)

    rows = pending_sheet.load_pending(result.pending_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["agol_format"] == "vector-tile-layer"

    # Both .gpkg and .vtpk now live in processing/<dataset_id>/.
    staging = project_tree["processing"] / row["dataset_id"]
    assert (staging / "parks.gpkg").exists()
    assert (staging / "parks.vtpk").exists()
    # queue/incoming/ was drained.
    assert not (project_tree["incoming"] / "parks.gpkg").exists()
    assert not (project_tree["incoming"] / "parks.vtpk").exists()


def test_scan_unpaired_vtpk_still_falls_through_to_existing_row_match(
    project_tree, valid_gpkg_factory,
) -> None:
    """A .vtpk whose stem doesn't match any GPKG being scanned still
    falls through to agol_vtpk.ingest_one_vtpk for matching against
    the existing catalogue. The pair-detection logic doesn't change
    the existing rev 3 unmatched-VTPK behaviour."""
    # Drop a lone .vtpk (no matching GPKG in this scan).
    (project_tree["incoming"] / "loner.vtpk").write_bytes(b"PK\x03\x04fake-vtpk")

    result = ingest.scan(
        project_tree["incoming"],
        project_tree["processing"],
        project_tree["rejected"],
        library_root=project_tree["library"],
        db_path=project_tree["db"],
    )
    assert result.accepted == 0
    # No catalogue row exists for 'loner', so unmatched.
    assert len(result.vtpk_results) == 1
    assert result.vtpk_results[0].status == "unmatched"
    # File stays in queue (not consumed by pairing).
    assert (project_tree["incoming"] / "loner.vtpk").exists()
