"""Tests for pipeline.agol_vtpk.

arcpy isn't pip-installable and isn't present in this test
environment. The tests stub it via ``sys.modules['arcpy']`` for the
build-path tests, and exercise the no-arcpy path by ensuring the
fallback raises a clean AgolToolingError.

Cache-hit tests don't need arcpy at all — they just write a
pre-existing .vtpk + sidecar into the cache and confirm the
function short-circuits.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pipeline import agol_vtpk
from pipeline.agol_sync import AgolToolingError


def _install_fake_arcpy(monkeypatch: pytest.MonkeyPatch, fake) -> None:
    """Inject a fake arcpy into sys.modules so `import arcpy` succeeds."""
    monkeypatch.setitem(sys.modules, "arcpy", fake)


# --- arcpy availability ------------------------------------------------

def test_is_arcpy_available_when_absent() -> None:
    """In this environment, arcpy is genuinely not installed."""
    assert agol_vtpk.is_arcpy_available() is False


def test_is_arcpy_available_when_stubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stub module makes the probe return True."""
    fake = types.ModuleType("arcpy")
    _install_fake_arcpy(monkeypatch, fake)
    assert agol_vtpk.is_arcpy_available() is True


def test_require_arcpy_raises_tooling_error_when_absent(tmp_path: Path) -> None:
    """build_vtpk's underlying import fail → AgolToolingError."""
    with pytest.raises(AgolToolingError, match="ArcGIS Pro"):
        agol_vtpk.build_vtpk(
            gpkg_path=tmp_path / "missing.gpkg",
            dataset_id="ds_test",
            checksum="abc",
            cache_dir=tmp_path / ".y2y",
        )


# --- cache hit ---------------------------------------------------------

def test_build_vtpk_cache_hit_skips_arcpy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing .vtpk + matching checksum sidecar → return without arcpy."""
    cache = tmp_path / ".y2y"
    vtpk_dir = cache / "vtpk_cache"
    vtpk_dir.mkdir(parents=True)
    vtpk = vtpk_dir / "ds_cached.vtpk"
    vtpk.write_bytes(b"fake-vtpk")
    vtpk.with_suffix(".vtpk.sha256").write_text("hash_v1", encoding="utf-8")

    # Stub arcpy as None — the cache hit should never reach the
    # arcpy import. We assert by NOT installing a fake arcpy: if the
    # function tries to import arcpy, AgolToolingError fires.
    out = agol_vtpk.build_vtpk(
        gpkg_path=tmp_path / "irrelevant.gpkg",   # not opened on cache hit
        dataset_id="ds_cached",
        checksum="hash_v1",
        cache_dir=cache,
    )
    assert out == vtpk
    assert out.read_bytes() == b"fake-vtpk"


def test_build_vtpk_cache_miss_when_checksum_differs(
    tmp_path: Path,
) -> None:
    """Pre-existing .vtpk but mismatched checksum → rebuild required.

    Without arcpy stubbed, this should raise AgolToolingError
    (proving the cache validity guard correctly detected the miss).
    """
    cache = tmp_path / ".y2y"
    vtpk_dir = cache / "vtpk_cache"
    vtpk_dir.mkdir(parents=True)
    vtpk = vtpk_dir / "ds_stale.vtpk"
    vtpk.write_bytes(b"stale")
    vtpk.with_suffix(".vtpk.sha256").write_text("hash_v1", encoding="utf-8")

    with pytest.raises(AgolToolingError):
        agol_vtpk.build_vtpk(
            gpkg_path=tmp_path / "irrelevant.gpkg",
            dataset_id="ds_stale",
            checksum="hash_v2_new",   # different from cached
            cache_dir=cache,
        )


# --- build path (arcpy stubbed) ----------------------------------------

def test_build_vtpk_invokes_arcpy_create_vtpk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With arcpy stubbed, build_vtpk drives the expected arcpy calls and
    writes a sidecar."""
    cache = tmp_path / ".y2y"
    gpkg = tmp_path / "src.gpkg"
    gpkg.write_bytes(b"fake-gpkg")  # build_vtpk only checks .exists()

    # The fake arcpy:
    #  - env.workspace setter accepts arbitrary path
    #  - ListFeatureClasses returns ['src'] (single layer)
    #  - management.CreateVectorTilePackage actually writes the
    #    .vtpk file (so the post-call existence check passes)
    fake = types.ModuleType("arcpy")
    fake.env = types.SimpleNamespace(workspace=None)
    fake.ListFeatureClasses = MagicMock(return_value=["src"])

    create_calls = []

    def _fake_create(
        in_map, output_file, service_type, tile_structure,
        min_cached_scale, max_cached_scale, index_polygons,
        summary, tags,
    ):
        create_calls.append({
            "in_map": in_map,
            "output_file": output_file,
            "service_type": service_type,
            "tile_structure": tile_structure,
            "min_cached_scale": min_cached_scale,
            "max_cached_scale": max_cached_scale,
            "summary": summary,
            "tags": tags,
        })
        Path(output_file).write_bytes(b"\x50\x4b\x03\x04fake-vtpk-payload")  # ZIP magic header

    fake.management = types.SimpleNamespace(
        CreateVectorTilePackage=_fake_create
    )
    _install_fake_arcpy(monkeypatch, fake)

    out = agol_vtpk.build_vtpk(
        gpkg_path=gpkg,
        dataset_id="ds_built",
        checksum="hash_v1",
        cache_dir=cache,
    )

    # File landed at the expected path.
    assert out.exists()
    assert out == cache / "vtpk_cache" / "ds_built.vtpk"

    # Sidecar records the checksum.
    sidecar = out.with_suffix(".vtpk.sha256")
    assert sidecar.read_text(encoding="utf-8") == "hash_v1"

    # arcpy was driven with the right inputs.
    assert len(create_calls) == 1
    call = create_calls[0]
    assert call["in_map"] == f"{gpkg}\\main.src"
    assert call["output_file"] == str(out)
    assert call["service_type"] == "ONLINE"
    assert call["tile_structure"] == "INDEXED"
    assert call["tags"] == "Y2Y"


def test_build_vtpk_rejects_multi_layer_gpkg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Y2Y canonical convention is single-layer GPKG; multi-layer is rejected."""
    gpkg = tmp_path / "multi.gpkg"
    gpkg.write_bytes(b"fake-gpkg")
    cache = tmp_path / ".y2y"

    fake = types.ModuleType("arcpy")
    fake.env = types.SimpleNamespace(workspace=None)
    fake.ListFeatureClasses = MagicMock(return_value=["layer_a", "layer_b"])
    fake.management = types.SimpleNamespace(
        CreateVectorTilePackage=MagicMock()
    )
    _install_fake_arcpy(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="single-layer"):
        agol_vtpk.build_vtpk(
            gpkg_path=gpkg,
            dataset_id="ds_multi",
            checksum="x",
            cache_dir=cache,
        )
    # arcpy.management.CreateVectorTilePackage was not called.
    fake.management.CreateVectorTilePackage.assert_not_called()


def test_build_vtpk_rejects_empty_gpkg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GPKG with no feature classes → RuntimeError."""
    gpkg = tmp_path / "empty.gpkg"
    gpkg.write_bytes(b"fake-gpkg")
    cache = tmp_path / ".y2y"

    fake = types.ModuleType("arcpy")
    fake.env = types.SimpleNamespace(workspace=None)
    fake.ListFeatureClasses = MagicMock(return_value=[])
    fake.management = types.SimpleNamespace(
        CreateVectorTilePackage=MagicMock()
    )
    _install_fake_arcpy(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="no feature classes"):
        agol_vtpk.build_vtpk(
            gpkg_path=gpkg,
            dataset_id="ds_empty",
            checksum="x",
            cache_dir=cache,
        )


def test_build_vtpk_rejects_missing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nonexistent GPKG → RuntimeError before arcpy is touched."""
    cache = tmp_path / ".y2y"
    # arcpy stubbed so the cache-miss path proceeds past the import.
    fake = types.ModuleType("arcpy")
    fake.env = types.SimpleNamespace(workspace=None)
    fake.ListFeatureClasses = MagicMock()
    fake.management = types.SimpleNamespace(
        CreateVectorTilePackage=MagicMock()
    )
    _install_fake_arcpy(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="source GeoPackage not found"):
        agol_vtpk.build_vtpk(
            gpkg_path=tmp_path / "missing.gpkg",
            dataset_id="ds_ghost",
            checksum="x",
            cache_dir=cache,
        )
