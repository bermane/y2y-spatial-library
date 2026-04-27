"""Shared fixtures for ingestion tests.

Generates minimal valid Y2Y datasets (a single-feature GeoPackage in
ESRI:102008, and a small Cloud Optimized GeoTIFF with the canonical
profile) at runtime so we don't need binary fixtures committed.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.enums import Resampling
from shapely.geometry import Point


@pytest.fixture
def valid_gpkg_factory(tmp_path: Path):
    """Return a factory that writes a valid Y2Y GeoPackage and returns its path.

    Usage:
        path = valid_gpkg_factory("streams_2024.gpkg")
    """

    def _make(filename: str, dest_dir: Path | None = None) -> Path:
        dest = (dest_dir or tmp_path) / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        gdf = gpd.GeoDataFrame(
            {"id": [1]},
            geometry=[Point(-1_500_000.0, 1_500_000.0)],
            crs="ESRI:102008",
        )
        gdf.to_file(dest, driver="GPKG")
        return dest

    return _make


@pytest.fixture
def valid_cog_factory(tmp_path: Path):
    """Factory for valid Y2Y Cloud Optimized GeoTIFFs.

    Default produces a 1024×1024 UInt8 raster with NoData=255, ZSTD,
    predictor 2, 512×512 blocks, one overview level. Override any of
    those via kwargs.
    """

    def _make(
        filename: str,
        *,
        dest_dir: Path | None = None,
        dtype: str = "uint8",
        nodata: int | float = 255,
        blockxsize: int = 512,
        blockysize: int = 512,
        compress: str = "zstd",
        predictor: int | None = None,
        build_overviews: bool = True,
        crs: str = "ESRI:102008",
    ) -> Path:
        dest = (dest_dir or tmp_path) / filename
        dest.parent.mkdir(parents=True, exist_ok=True)

        w = h = 1024
        if dtype == "uint8":
            data = np.full((h, w), int(nodata), dtype="uint8")
            default_predictor = 2
        elif dtype == "uint16":
            data = np.full((h, w), int(nodata), dtype="uint16")
            default_predictor = 2
        elif dtype == "float32":
            data = np.full((h, w), float(nodata), dtype="float32")
            default_predictor = 3
        elif dtype == "int32":
            data = np.full((h, w), int(nodata), dtype="int32")
            default_predictor = 2  # arbitrary; this dtype is not allowed
        else:
            raise ValueError(f"unsupported test dtype: {dtype}")

        chosen_predictor = predictor if predictor is not None else default_predictor

        profile: dict[str, object] = {
            "driver": "GTiff",
            "dtype": dtype,
            "count": 1,
            "width": w,
            "height": h,
            "crs": crs,
            "transform": rasterio.transform.from_origin(-1_500_000.0, 1_500_000.0, 100.0, 100.0),
            "tiled": True,
            "blockxsize": blockxsize,
            "blockysize": blockysize,
            "nodata": nodata,
            "BIGTIFF": "IF_NEEDED",
        }
        if compress:
            profile["compress"] = compress
            profile["predictor"] = chosen_predictor
            if compress == "zstd":
                profile["zstd_level"] = 9

        with rasterio.open(dest, "w", **profile) as dst:
            dst.write(data, 1)
            if build_overviews:
                dst.build_overviews([2], Resampling.nearest)

        return dest

    return _make


@pytest.fixture
def project_tree(tmp_path: Path) -> dict[str, Path]:
    """Create the standard Y2Y directory layout under tmp_path."""
    paths = {
        "root": tmp_path,
        "incoming": tmp_path / "queue" / "incoming",
        "processing": tmp_path / "queue" / "processing",
        "rejected": tmp_path / "queue" / "rejected",
        "library": tmp_path / "library",
        "inventory": tmp_path / "inventory" / "inventory.xlsx",
        "changelog": tmp_path / "inventory" / "changelog.md",
    }
    for key in ("incoming", "processing", "rejected", "library"):
        paths[key].mkdir(parents=True, exist_ok=True)
    paths["inventory"].parent.mkdir(parents=True, exist_ok=True)
    return paths


@pytest.fixture
def populate_dataset(project_tree, valid_gpkg_factory):
    """End-to-end factory: scan + fill + approve. Returns a tuple
    ``(dataset_id, library_relative_path)`` for an ingested dataset.
    """
    from pipeline import ingest, pending_sheet

    def _populate(
        filename: str = "streams_2024.gpkg",
        *,
        category: str = "Water",  # Water's display name == folder name
        subcategory: str | None = None,
        steward: str = "Tester",
    ) -> tuple[str, str]:
        valid_gpkg_factory(filename, dest_dir=project_tree["incoming"])
        ingest.scan(project_tree["incoming"], project_tree["processing"], project_tree["rejected"])

        pending_path = project_tree["processing"] / pending_sheet.PENDING_FILENAME
        rows = pending_sheet.load_pending(pending_path)
        # Pick the row whose source_filename matches what we just dropped
        row = next(r for r in rows if r["source_filename"] == filename)
        row.update(
            ready=True,
            category=category,
            subcategory=subcategory,
            data_steward=steward,
            title="Test Title",
            summary="Test summary.",
            description="Test description.",
            tags="test",
            terms_of_use="Test terms.",
            acknowledgements="Test ack.",
        )
        # Replace just this row in the persisted sheet (preserve siblings).
        all_rows = pending_sheet.load_pending(pending_path)
        for i, r in enumerate(all_rows):
            if r.get("dataset_id") == row["dataset_id"]:
                all_rows[i] = row
        pending_sheet.save_pending(pending_path, all_rows)

        ingest.approve(
            project_tree["processing"], project_tree["library"],
            project_tree["inventory"], project_tree["changelog"],
            actor="tester",
        )

        # target_filename auto-proposes from the source stem. The library
        # path uses *folder* names; the inventory's category is the
        # *display* name. Convert here for the returned filesystem rel.
        from pipeline import taxonomy
        target_filename = row["target_filename"]
        cat_folder = taxonomy.CATEGORY_FOLDERS.get(category, category)
        if subcategory:
            sub_folder = taxonomy.SUBCATEGORY_FOLDERS.get(category, {}).get(
                subcategory, subcategory,
            )
            rel = f"{cat_folder}/{sub_folder}/{target_filename}"
        else:
            rel = f"{cat_folder}/{target_filename}"
        return row["dataset_id"], rel

    return _populate
