"""Naming-convention validator tests (no GIS deps required)."""

from __future__ import annotations

from pathlib import Path

from pipeline.validators.naming import validate_naming


def test_accepts_lowercase_underscore_gpkg() -> None:
    ok, reason = validate_naming(Path("grizzly_bear_den_sites_2024.gpkg"))
    assert ok and reason is None


def test_accepts_lowercase_underscore_tif() -> None:
    ok, _ = validate_naming(Path("dem_100m.tif"))
    assert ok


def test_rejects_uppercase_letters() -> None:
    ok, reason = validate_naming(Path("Grizzly_Bear.gpkg"))
    assert not ok
    assert reason and "lowercase" in reason


def test_rejects_spaces() -> None:
    ok, _ = validate_naming(Path("grizzly bear.gpkg"))
    assert not ok


def test_rejects_hyphens() -> None:
    ok, _ = validate_naming(Path("grizzly-bear.gpkg"))
    assert not ok


def test_rejects_double_underscore() -> None:
    ok, _ = validate_naming(Path("grizzly__bear.gpkg"))
    assert not ok


def test_rejects_shapefile_extension() -> None:
    ok, _ = validate_naming(Path("data.shp"))
    assert not ok


def test_rejects_no_extension() -> None:
    ok, _ = validate_naming(Path("grizzly_bear"))
    assert not ok


def test_rejects_leading_underscore() -> None:
    ok, _ = validate_naming(Path("_grizzly_bear.gpkg"))
    assert not ok
