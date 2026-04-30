"""Tests for source→canonical transformations.

Vector: format conversion + reprojection + geometry-type promotion.
Raster: format-canonicalisation + reprojection + correct predictor /
resampling per classification + NoData defaults.
"""

from __future__ import annotations

from pathlib import Path

import fiona
import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.enums import Resampling
from shapely.geometry import (
    LineString, MultiLineString, MultiPolygon, Point, Polygon,
)

from pipeline import transformations


# --- vector --------------------------------------------------------------

def test_vector_reprojects_to_canonical_crs(tmp_path: Path) -> None:
    src = tmp_path / "wgs84.shp"
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(-120.0, 50.0)], crs="EPSG:4326")
    gdf.to_file(src, driver="ESRI Shapefile")

    target = tmp_path / "out.gpkg"
    transformations.vector_to_canonical(src, target)

    out = gpd.read_file(target)
    assert out.crs.to_authority() == ("ESRI", "102008")
    assert len(out) == 1


def test_vector_passthrough_when_already_canonical(tmp_path: Path, valid_gpkg_factory) -> None:
    src = valid_gpkg_factory("ok.gpkg")
    target = tmp_path / "out.gpkg"
    transformations.vector_to_canonical(src, target)
    assert target.exists()
    assert gpd.read_file(target).crs.to_authority() == ("ESRI", "102008")


def test_vector_passthrough_is_byte_identical(tmp_path: Path, valid_gpkg_factory) -> None:
    """Already-canonical GPKG should pass through via shutil.copy2, not be re-encoded."""
    src = valid_gpkg_factory("ok.gpkg")
    target = tmp_path / "out.gpkg"
    transformations.vector_to_canonical(src, target)
    assert target.read_bytes() == src.read_bytes()


def test_passthrough_eligibility_check(tmp_path: Path, valid_gpkg_factory) -> None:
    """is_passthrough_eligible returns True for canonical sources, False otherwise."""
    canonical = valid_gpkg_factory("canonical.gpkg")
    assert transformations.is_passthrough_eligible(canonical)

    # A Shapefile in EPSG:4326 — wrong format AND wrong CRS
    shp = tmp_path / "wrong.shp"
    gpd.GeoDataFrame(
        {"id": [1]}, geometry=[Point(-120, 50)], crs="EPSG:4326"
    ).to_file(shp, driver="ESRI Shapefile")
    assert not transformations.is_passthrough_eligible(shp)


def test_vector_promotes_mixed_polygon_multipolygon(tmp_path: Path) -> None:
    src = tmp_path / "mixed.gpkg"
    geoms = [
        Polygon([(0, 0), (1, 0), (1, 1), (0, 0)]),
        MultiPolygon([Polygon([(2, 2), (3, 2), (3, 3), (2, 2)])]),
    ]
    gpd.GeoDataFrame({"id": [1, 2]}, geometry=geoms, crs="ESRI:102008").to_file(src, driver="GPKG")

    target = tmp_path / "out.gpkg"
    transformations.vector_to_canonical(src, target)

    with fiona.open(target) as fc:
        assert fc.schema["geometry"] == "MultiPolygon"


def test_vector_rejects_incompatible_mixed_geometry(tmp_path: Path) -> None:
    src = tmp_path / "bad_mix.gpkg"
    geoms = [Point(0, 0), LineString([(0, 0), (1, 1)])]
    # GPKG won't accept a layer with mixed Point+LineString as a single concrete
    # type, so write GeoJSON which can.
    src = tmp_path / "bad_mix.geojson"
    gpd.GeoDataFrame({"id": [1, 2]}, geometry=geoms, crs="EPSG:4326").to_file(src, driver="GeoJSON")

    target = tmp_path / "out.gpkg"
    with pytest.raises(transformations.TransformError, match="incompatible mixed geometry"):
        transformations.vector_to_canonical(src, target)


def test_vector_rejects_invalid_geometry(tmp_path: Path) -> None:
    src = tmp_path / "bowtie.gpkg"
    bowtie = Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)])
    gpd.GeoDataFrame({"id": [1]}, geometry=[bowtie], crs="ESRI:102008").to_file(src, driver="GPKG")

    target = tmp_path / "out.gpkg"
    with pytest.raises(transformations.TransformError, match="invalid geometries"):
        transformations.vector_to_canonical(src, target)


def test_vector_requires_crs_or_override(tmp_path: Path) -> None:
    """A source without CRS must be ingested with source_crs_override declared."""
    src = tmp_path / "nocrs.geojson"
    src.write_text(
        '{"type":"FeatureCollection","features":['
        '{"type":"Feature","properties":{"id":1},'
        '"geometry":{"type":"Point","coordinates":[0,0]}}]}'
    )

    target = tmp_path / "out.gpkg"
    # Some GeoJSON readers default to WGS84; if so, no override is needed.
    # If it returns None, vector_to_canonical raises TransformError.
    try:
        transformations.vector_to_canonical(src, target)
    except transformations.TransformError as exc:
        assert "no CRS" in str(exc)
    else:
        # Default-CRS path: target must end up canonical.
        assert gpd.read_file(target).crs.to_authority() == ("ESRI", "102008")


def test_vector_without_crs_is_rejected_defensively(tmp_path: Path) -> None:
    """Scan rejects CRS-less sources; this is a defensive check at the
    transformation layer in case scan was bypassed. CRS-override workflow
    no longer exists (DESIGN.md §11)."""
    src = tmp_path / "nocrs.geojson"
    src.write_text(
        '{"type":"FeatureCollection","features":['
        '{"type":"Feature","properties":{"id":1},'
        '"geometry":{"type":"Point","coordinates":[-120,50]}}]}'
    )

    target = tmp_path / "out.gpkg"
    # GeoJSON sometimes defaults to WGS84; the transformer should accept
    # those and reproject. We only assert it doesn't error with the
    # "should have been rejected at scan" message *if* a CRS was found.
    try:
        transformations.vector_to_canonical(src, target)
    except transformations.TransformError as exc:
        assert "no CRS" in str(exc) or "should have been rejected" in str(exc)
    else:
        assert gpd.read_file(target).crs.to_authority() == ("ESRI", "102008")


# --- raster --------------------------------------------------------------

def test_raster_passthrough_when_already_canonical(tmp_path: Path, valid_cog_factory) -> None:
    """Already-canonical COG is copied byte-for-byte (no re-encode)."""
    src = valid_cog_factory("src.tif", dtype="float32", nodata=-9999.0)
    target = tmp_path / "out.tif"
    transformations.raster_to_canonical(src, target, "continuous")
    assert target.read_bytes() == src.read_bytes()


def test_raster_passthrough_rejects_classification_mismatch(
    tmp_path: Path, valid_cog_factory,
) -> None:
    """Float32 source declared 'categorical' (predictor 2) is rejected — predictors disagree."""
    src = valid_cog_factory("src.tif", dtype="float32", nodata=-9999.0)
    with pytest.raises(transformations.TransformError, match="inconsistent with declared classification"):
        transformations.raster_to_canonical(src, tmp_path / "out.tif", "categorical")


def test_raster_continuous_uses_bilinear_and_predictor3(tmp_path: Path, valid_cog_factory) -> None:
    src = valid_cog_factory("src.tif", dtype="float32", nodata=-9999.0)
    target = tmp_path / "out.tif"
    transformations.raster_to_canonical(src, target, "continuous")

    with rasterio.open(target) as ds:
        assert ds.crs.to_authority() == ("ESRI", "102008")
        assert ds.profile["dtype"] == "float32"
        assert ds.profile["compress"].lower() == "zstd"
        struct = ds.tags(ns="IMAGE_STRUCTURE") or {}
        assert struct.get("PREDICTOR") == "3"
        assert ds.nodata == -9999.0
        assert ds.profile["blockxsize"] == 512


def test_raster_categorical_uses_nearest_and_predictor2(tmp_path: Path, valid_cog_factory) -> None:
    src = valid_cog_factory("src.tif", dtype="uint8", nodata=255)
    target = tmp_path / "out.tif"
    transformations.raster_to_canonical(src, target, "categorical")

    with rasterio.open(target) as ds:
        assert ds.crs.to_authority() == ("ESRI", "102008")
        assert ds.profile["dtype"] == "uint8"
        struct = ds.tags(ns="IMAGE_STRUCTURE") or {}
        assert struct.get("PREDICTOR") == "2"
        assert ds.nodata == 255


def test_raster_reprojects_to_canonical(tmp_path: Path) -> None:
    src = tmp_path / "wgs.tif"
    profile = {
        "driver": "GTiff", "dtype": "uint8", "count": 1,
        "width": 1024, "height": 1024, "crs": "EPSG:4326",
        "transform": rasterio.transform.from_origin(-120, 50, 0.001, 0.001),
        "tiled": True, "blockxsize": 512, "blockysize": 512,
        "compress": "zstd", "predictor": 2, "zstd_level": 9,
        "nodata": 255, "BIGTIFF": "IF_NEEDED",
    }
    with rasterio.open(src, "w", **profile) as dst:
        dst.write(np.zeros((1024, 1024), dtype="uint8"), 1)
        dst.build_overviews([2], Resampling.nearest)

    target = tmp_path / "out.tif"
    transformations.raster_to_canonical(src, target, "categorical")
    with rasterio.open(target) as ds:
        assert ds.crs.to_authority() == ("ESRI", "102008")


def test_raster_preserves_source_pixel_size_on_reprojection(tmp_path: Path) -> None:
    """A 30m source must produce a clean 30.0m destination, not 30.06938…m.

    Without an explicit ``dst_resolution`` argument, rasterio's
    ``calculate_default_transform`` fits the destination grid to the
    projected bounds and produces a pixel size that depends on how the
    bounds project — typically off by a fraction of a metre. The
    pipeline forces ``dst_resolution=(src.transform.a, |src.transform.e|)``
    so the round-number resolution survives reprojection. This is the
    regression for the GB-habitat (EPSG:26911 → ESRI:102008) case where
    the warp produced 30.06938…m pixels.
    """
    src = tmp_path / "utm.tif"
    profile = {
        "driver": "GTiff", "dtype": "uint8", "count": 1,
        "width": 1024, "height": 1024, "crs": "EPSG:26911",  # UTM 11N, metres
        # Origin in UTM 11N metres; 30 m pixels (north-up: y-spacing negative).
        "transform": rasterio.transform.from_origin(500_000.0, 5_500_000.0, 30.0, 30.0),
        "tiled": True, "blockxsize": 512, "blockysize": 512,
        "compress": "zstd", "predictor": 2, "zstd_level": 9,
        "nodata": 255, "BIGTIFF": "IF_NEEDED",
    }
    with rasterio.open(src, "w", **profile) as dst:
        dst.write(np.zeros((1024, 1024), dtype="uint8"), 1)
        dst.build_overviews([2], Resampling.nearest)

    target = tmp_path / "out.tif"
    transformations.raster_to_canonical(src, target, "categorical")

    with rasterio.open(target) as ds:
        assert ds.crs.to_authority() == ("ESRI", "102008")
        # transform.a is x-spacing, transform.e is y-spacing (negative
        # for north-up). Both must equal the source's 30 m exactly.
        assert ds.transform.a == 30.0
        assert ds.transform.e == -30.0


def test_raster_rejects_unsupported_dtype(tmp_path: Path) -> None:
    src = tmp_path / "int32.tif"
    profile = {
        "driver": "GTiff", "dtype": "int32", "count": 1,
        "width": 256, "height": 256, "crs": "ESRI:102008",
        "transform": rasterio.transform.from_origin(0, 0, 100, 100),
        "tiled": True, "blockxsize": 256, "blockysize": 256,
        "nodata": -1,
    }
    with rasterio.open(src, "w", **profile) as dst:
        dst.write(np.zeros((256, 256), dtype="int32"), 1)

    target = tmp_path / "out.tif"
    with pytest.raises(transformations.TransformError, match="dtype"):
        transformations.raster_to_canonical(src, target, "continuous")


def test_raster_rejects_invalid_classification(tmp_path: Path, valid_cog_factory) -> None:
    src = valid_cog_factory("src.tif")
    with pytest.raises(transformations.TransformError, match="classification"):
        transformations.raster_to_canonical(src, tmp_path / "out.tif", "neither")


def test_raster_uses_default_nodata_when_source_has_none(tmp_path: Path) -> None:
    src = tmp_path / "no_nd.tif"
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1,
        "width": 1024, "height": 1024, "crs": "ESRI:102008",
        "transform": rasterio.transform.from_origin(0, 0, 100, 100),
        "tiled": True, "blockxsize": 512, "blockysize": 512,
        "compress": "zstd", "predictor": 3, "zstd_level": 9,
    }
    with rasterio.open(src, "w", **profile) as dst:
        dst.write(np.zeros((1024, 1024), dtype="float32"), 1)
        dst.build_overviews([2], Resampling.bilinear)

    target = tmp_path / "out.tif"
    transformations.raster_to_canonical(src, target, "continuous")
    with rasterio.open(target) as ds:
        assert ds.nodata == -9999.0  # canonical default for float32
