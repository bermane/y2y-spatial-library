"""Tests for source-format detection and Phase-A acceptance rules."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon

from pipeline import source_formats


# --- detect / classification -------------------------------------------

def test_detect_recognizes_each_allow_listed_format() -> None:
    assert source_formats.detect(Path("a.shp"))[0] == "Shapefile"
    assert source_formats.detect(Path("a.gpkg"))[0] == "GeoPackage"
    assert source_formats.detect(Path("a.geojson"))[0] == "GeoJSON"
    assert source_formats.detect(Path("a.json"))[0] == "GeoJSON"
    assert source_formats.detect(Path("a.kml"))[0] == "KML"
    assert source_formats.detect(Path("a.kmz"))[0] == "KML"
    assert source_formats.detect(Path("a.tif"))[0] == "GeoTIFF"
    assert source_formats.detect(Path("a.tiff"))[0] == "GeoTIFF"


def test_detect_classifies_vector_vs_raster() -> None:
    assert source_formats.detect(Path("a.shp"))[1] is True
    assert source_formats.detect(Path("a.tif"))[1] is False


def test_detect_rejects_unknown_extension() -> None:
    with pytest.raises(KeyError):
        source_formats.detect(Path("a.csv"))


def test_is_recognized_handles_case() -> None:
    assert source_formats.is_recognized(Path("a.GPKG"))
    assert source_formats.is_recognized(Path("a.SHP"))
    assert not source_formats.is_recognized(Path("a.xyz"))


# --- shapefile bundle handling ----------------------------------------

def test_shapefile_bundle_collects_sidecars(tmp_path: Path) -> None:
    shp = tmp_path / "roads.shp"
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326")
    gdf.to_file(shp, driver="ESRI Shapefile")

    bundle = source_formats.shapefile_bundle(shp)
    suffixes = {p.suffix.lower() for p in bundle}
    # Always at least .shp + .shx + .dbf
    assert ".shp" in suffixes
    assert ".shx" in suffixes
    assert ".dbf" in suffixes


def test_raster_bundle_collects_aux_sidecars(tmp_path: Path, valid_cog_factory) -> None:
    """A .tif with a .tif.aux.xml sibling — both should be in the bundle."""
    tif = valid_cog_factory("habitat.tif", dest_dir=tmp_path)
    aux = tmp_path / "habitat.tif.aux.xml"
    aux.write_text("<PAMDataset/>")  # minimal valid PAM xml stub

    bundle = source_formats.raster_bundle(tif)
    names = {p.name for p in bundle}
    assert "habitat.tif" in names
    assert "habitat.tif.aux.xml" in names


def test_raster_bundle_ignores_unrelated_files(tmp_path: Path, valid_cog_factory) -> None:
    """Sibling files with different stems are NOT in the bundle."""
    tif = valid_cog_factory("habitat.tif", dest_dir=tmp_path)
    (tmp_path / "habitat.tif.aux.xml").write_text("<PAMDataset/>")
    (tmp_path / "different_name.tif.aux.xml").write_text("<PAMDataset/>")

    bundle = source_formats.raster_bundle(tif)
    names = {p.name for p in bundle}
    assert "different_name.tif.aux.xml" not in names


def test_is_shapefile_sidecar() -> None:
    assert source_formats.is_shapefile_sidecar(Path("roads.shx"))
    assert source_formats.is_shapefile_sidecar(Path("roads.dbf"))
    assert source_formats.is_shapefile_sidecar(Path("roads.prj"))
    assert not source_formats.is_shapefile_sidecar(Path("roads.shp"))
    assert not source_formats.is_shapefile_sidecar(Path("notes.txt"))


# --- inspect: single-layer / single-band rules ------------------------

def test_inspect_accepts_valid_single_layer_gpkg(valid_gpkg_factory) -> None:
    path = valid_gpkg_factory("good.gpkg")
    meta = source_formats.inspect(path)
    assert meta.is_vector
    assert meta.source_format == "GeoPackage"
    assert meta.source_crs == "ESRI:102008"
    assert meta.source_layer is None


def test_inspect_rejects_multi_layer_gpkg(tmp_path: Path) -> None:
    path = tmp_path / "multi.gpkg"
    gdf1 = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326")
    gdf2 = gpd.GeoDataFrame({"id": [2]}, geometry=[Point(1, 1)], crs="EPSG:4326")
    gdf1.to_file(path, driver="GPKG", layer="layer_a")
    gdf2.to_file(path, driver="GPKG", layer="layer_b")

    with pytest.raises(source_formats.SourceRejected, match="single-layer"):
        source_formats.inspect(path)


def test_inspect_rejects_mixed_geometry_geojson(tmp_path: Path) -> None:
    path = tmp_path / "mixed.geojson"
    geoms = [
        Point(0, 0),
        LineString([(0, 0), (1, 1)]),
        Polygon([(0, 0), (1, 0), (1, 1), (0, 0)]),
    ]
    gdf = gpd.GeoDataFrame({"id": [1, 2, 3]}, geometry=geoms, crs="EPSG:4326")
    gdf.to_file(path, driver="GeoJSON")

    with pytest.raises(source_formats.SourceRejected, match="mixed|geometry"):
        source_formats.inspect(path)


def test_inspect_accepts_valid_single_band_tiff(valid_cog_factory) -> None:
    path = valid_cog_factory("good.tif", dtype="uint8")
    meta = source_formats.inspect(path)
    assert not meta.is_vector
    assert meta.source_format == "GeoTIFF"
    assert meta.source_crs == "ESRI:102008"


def test_inspect_rejects_multi_band_tiff(tmp_path: Path) -> None:
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling

    path = tmp_path / "rgb.tif"
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

    with pytest.raises(source_formats.SourceRejected, match="single-band"):
        source_formats.inspect(path)


def test_inspect_falls_back_to_projection_name_when_no_authority(tmp_path: Path) -> None:
    """A custom CRS without authority code surfaces its name in source_crs."""
    import fiona
    from fiona.crs import CRS as FionaCRS

    # Y2Y-style custom Albers in WGS 84 — no authority code resolves.
    custom_wkt = (
        'PROJCS["WGS_1984_Albers",'
        'GEOGCS["WGS 84",DATUM["WGS_1984",'
        'SPHEROID["WGS 84",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],'
        'PROJECTION["Albers_Conic_Equal_Area"],'
        'PARAMETER["latitude_of_center",55],'
        'PARAMETER["longitude_of_center",-120],'
        'PARAMETER["standard_parallel_1",42],'
        'PARAMETER["standard_parallel_2",68],'
        'UNIT["metre",1]]'
    )
    path = tmp_path / "custom.shp"
    schema = {"geometry": "Polygon", "properties": {"id": "int"}}
    with fiona.open(
        path, "w", driver="ESRI Shapefile",
        crs=FionaCRS.from_wkt(custom_wkt),
        schema=schema,
    ) as dst:
        dst.write({
            "geometry": {
                "type": "Polygon",
                "coordinates": [[(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]],
            },
            "properties": {"id": 1},
        })

    meta = source_formats.inspect(path)
    # No authority resolves, so we expect the projection name (no colon).
    assert meta.source_crs is not None
    assert ":" not in meta.source_crs
    assert "Albers" in meta.source_crs


def test_inspect_rejects_source_with_no_crs(tmp_path: Path) -> None:
    """A vector source without a CRS is rejected at scan, not accepted with null."""
    path = tmp_path / "nocrs.geojson"
    # GeoJSON without CRS info: write raw JSON
    path.write_text(
        '{"type":"FeatureCollection","features":['
        '{"type":"Feature","properties":{"id":1},'
        '"geometry":{"type":"Point","coordinates":[0,0]}}]}'
    )

    # Some GeoJSON readers default to WGS84/CRS84; if the file ends up
    # with a CRS, scan accepts it. If it surfaces as null, scan must reject.
    try:
        meta = source_formats.inspect(path)
    except source_formats.SourceRejected as exc:
        assert "CRS" in str(exc)
        return
    # If no rejection, the reader resolved a CRS (likely CRS84/EPSG:4326).
    assert meta.source_crs is not None
    assert meta.is_vector


# --- candidate_paths -----------------------------------------------------

def test_candidate_paths_skips_sidecars_and_unknowns(tmp_path: Path, valid_gpkg_factory) -> None:
    valid_gpkg_factory("a.gpkg", dest_dir=tmp_path)
    # Drop a stray .shx (sidecar with no .shp leader) and an unknown file
    (tmp_path / "lonely.shx").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("ignore me")
    (tmp_path / ".DS_Store").write_bytes(b"")

    candidates = list(source_formats.candidate_paths(tmp_path))
    # Only the .gpkg should appear
    assert len(candidates) == 1
    assert candidates[0].name == "a.gpkg"
