"""Tests for the hardened format validator.

Covers:
- Vector: 2D-only, single concrete geometry type, geometry validity.
- Raster: GTiff driver, 512×512 tiling, overviews, ZSTD compression,
  predictor matching dtype, allowed dtype set, NoData defined.
"""

from __future__ import annotations

from pathlib import Path

import fiona
import geopandas as gpd
import pytest
from fiona.crs import CRS as FionaCRS
from shapely.geometry import Polygon

from pipeline.validators.format import validate_format


# ---- vector -----------------------------------------------------------

def test_valid_gpkg_passes(valid_gpkg_factory) -> None:
    path = valid_gpkg_factory("good.gpkg")
    ok, reason = validate_format(path)
    assert ok, reason


def test_3d_gpkg_rejected(tmp_path: Path) -> None:
    """A GPKG layer declared with 3D geometry is rejected."""
    path = tmp_path / "three_d.gpkg"
    schema = {"geometry": "3D Point", "properties": {"id": "int"}}
    with fiona.open(
        path, "w",
        driver="GPKG",
        crs=FionaCRS.from_user_input("ESRI:102008"),
        schema=schema,
    ) as dst:
        dst.write({
            "geometry": {"type": "Point", "coordinates": (0.0, 0.0, 100.0)},
            "properties": {"id": 1},
        })

    ok, reason = validate_format(path)
    assert not ok
    assert reason and ("Z" in reason or "3D" in reason)


def test_multipolygon_2d_is_not_falsely_rejected(tmp_path: Path) -> None:
    """Regression: 'M' substring in 'MultiPolygon' must NOT trigger Z/M rejection."""
    path = tmp_path / "polys.gpkg"
    poly_a = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
    poly_b = Polygon([(2, 2), (3, 2), (3, 3), (2, 3), (2, 2)])
    gdf = gpd.GeoDataFrame(
        {"id": [1]},
        geometry=[gpd.GeoSeries([poly_a, poly_b]).unary_union],
        crs="ESRI:102008",
    )
    gdf.to_file(path, driver="GPKG")

    ok, reason = validate_format(path)
    assert ok, reason


def test_z_on_features_rejected_even_when_schema_reads_2d(tmp_path: Path) -> None:
    """Schema may report plain MultiPolygon while features carry Z values."""
    path = tmp_path / "feat_z.gpkg"
    schema = {"geometry": "MultiPolygon", "properties": {"id": "int"}}
    # fiona accepts Z coordinates even if schema doesn't declare them in some
    # configurations; if not, fall through and skip this assertion path.
    try:
        with fiona.open(
            path, "w",
            driver="GPKG",
            crs=FionaCRS.from_user_input("ESRI:102008"),
            schema=schema,
        ) as dst:
            dst.write({
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [[[(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 0, 1)]]],
                },
                "properties": {"id": 1},
            })
    except Exception:
        pytest.skip("fiona refused to write Z under a 2D schema; OS-level test")

    ok, reason = validate_format(path)
    # Either schema-level or feature-level catch is acceptable
    assert not ok
    assert reason and ("Z" in reason or "3D" in reason)


def test_invalid_geometry_rejected(tmp_path: Path) -> None:
    """A self-intersecting polygon (bowtie) is invalid and must be rejected."""
    path = tmp_path / "bowtie.gpkg"
    bowtie = Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)])
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[bowtie], crs="ESRI:102008")
    gdf.to_file(path, driver="GPKG")

    ok, reason = validate_format(path)
    assert not ok
    assert reason and "invalid geometry" in reason


# ---- raster -----------------------------------------------------------

def test_valid_uint8_cog_passes(valid_cog_factory) -> None:
    path = valid_cog_factory("good_uint8.tif")
    ok, reason = validate_format(path)
    assert ok, reason


def test_valid_float32_cog_passes(valid_cog_factory) -> None:
    path = valid_cog_factory("good_float32.tif", dtype="float32", nodata=-9999)
    ok, reason = validate_format(path)
    assert ok, reason


def test_disallowed_dtype_rejected(valid_cog_factory) -> None:
    path = valid_cog_factory("int32.tif", dtype="int32", nodata=-1)
    ok, reason = validate_format(path)
    assert not ok
    assert reason and "dtype" in reason


def test_wrong_predictor_rejected(valid_cog_factory) -> None:
    # uint8 should use predictor=2; force predictor=1 (no prediction) to fail.
    path = valid_cog_factory("bad_predictor.tif", dtype="uint8", predictor=1)
    ok, reason = validate_format(path)
    assert not ok
    assert reason and "predictor" in reason


def test_missing_compression_rejected(valid_cog_factory) -> None:
    path = valid_cog_factory("uncompressed.tif", compress="")
    ok, reason = validate_format(path)
    assert not ok
    assert reason and ("compression" in reason.lower() or "zstd" in reason.lower())


def test_wrong_block_size_rejected(valid_cog_factory) -> None:
    path = valid_cog_factory("256_blocks.tif", blockxsize=256, blockysize=256)
    ok, reason = validate_format(path)
    assert not ok
    assert reason and "block size" in reason


def test_no_overviews_rejected(valid_cog_factory) -> None:
    path = valid_cog_factory("no_overviews.tif", build_overviews=False)
    ok, reason = validate_format(path)
    assert not ok
    assert reason and "overviews" in reason


def test_no_nodata_rejected(valid_cog_factory, tmp_path: Path) -> None:
    """Build a raster with the correct shape but no NoData defined."""
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling

    path = tmp_path / "no_nodata.tif"
    profile = {
        "driver": "GTiff", "dtype": "uint8", "count": 1,
        "width": 1024, "height": 1024, "crs": "ESRI:102008",
        "transform": rasterio.transform.from_origin(0, 0, 100, 100),
        "tiled": True, "blockxsize": 512, "blockysize": 512,
        "compress": "zstd", "predictor": 2, "zstd_level": 9,
        "BIGTIFF": "IF_NEEDED",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.zeros((1024, 1024), dtype="uint8"), 1)
        dst.build_overviews([2], Resampling.nearest)

    ok, reason = validate_format(path)
    assert not ok
    assert reason and "NoData" in reason
