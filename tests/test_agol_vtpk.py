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


def _stub_pro_project_plumbing(
    fake_arcpy: types.ModuleType, tmp_path: Path,
) -> tuple[MagicMock, MagicMock]:
    """Wire the fake arcpy with a working .mp.ArcGISProject + GetInstallInfo.

    build_vtpk needs:

    * ``arcpy.GetInstallInfo()['InstallDir']`` → an install dir we
      can plant a fake BlankTemplate.aprx under.
    * ``arcpy.mp.ArcGISProject(template_path)`` → returns a project
      with a saveACopy method that creates the destination file.
    * ``arcpy.mp.ArcGISProject(dest_path)`` → returns a project
      with listMaps/createMap/save/addDataFromPath methods.

    Returns the (project, map) mocks so individual tests can assert
    against them.
    """
    install_dir = tmp_path / "fake_pro_install"
    # Plant the template at Pro 3.x's actual bundled location (the
    # routing-services data folder, an unusual but real location).
    template_dir = install_dir / "Resources" / "ArcToolBox" / "Services" / "routingservices" / "data"
    template_dir.mkdir(parents=True)
    template_path = template_dir / "Blank.aprx"
    template_path.write_bytes(b"fake-template")

    fake_arcpy.GetInstallInfo = MagicMock(
        return_value={"InstallDir": str(install_dir)}
    )

    map_mock = MagicMock()
    map_mock.addDataFromPath = MagicMock()
    project_mock = MagicMock()
    project_mock.listMaps = MagicMock(return_value=[map_mock])
    project_mock.save = MagicMock()
    # saveACopy must actually create the destination so subsequent
    # ArcGISProject(dest) calls in production code don't crash on
    # missing-file. Our build_vtpk calls saveACopy then re-opens
    # the same path.
    def _save_a_copy(dest):
        Path(dest).write_bytes(b"fake-staging-aprx")
    project_mock.saveACopy = MagicMock(side_effect=_save_a_copy)

    fake_arcpy.mp = types.SimpleNamespace(
        ArcGISProject=MagicMock(return_value=project_mock)
    )
    return project_mock, map_mock


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

    def _fake_create(**kwargs):
        # build_vtpk now passes scale arguments only when explicitly
        # provided (default omits them so arcpy uses the tiling
        # scheme's natural range). Accept any kwargs.
        create_calls.append(kwargs)
        Path(kwargs["output_file"]).write_bytes(
            b"\x50\x4b\x03\x04fake-vtpk-payload"  # ZIP magic header
        )

    fake.management = types.SimpleNamespace(
        CreateVectorTilePackage=_fake_create
    )
    project_mock, map_mock = _stub_pro_project_plumbing(fake, tmp_path)
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
    # in_map is now the Map object from the staging project, NOT a
    # feature class path (arcpy 3.x requires a real Map).
    assert call["in_map"] is map_mock
    assert call["output_file"] == str(out)
    assert call["service_type"] == "ONLINE"
    assert call["tile_structure"] == "INDEXED"
    assert call["tags"] == "Y2Y"

    # REGRESSION GUARD (Test 3 ERROR 001856): by default build_vtpk
    # must NOT pass min_cached_scale or max_cached_scale to arcpy.
    # arcpy validates these against the active tiling scheme with
    # exact-float comparison and rounding to 6 decimals fails.
    # Omitting them lets arcpy use the scheme's natural range.
    assert "min_cached_scale" not in call
    assert "max_cached_scale" not in call

    # The GPKG layer was added to the staging map and the project
    # was saved before CreateVectorTilePackage ran.
    map_mock.addDataFromPath.assert_called_once_with(f"{gpkg}\\main.src")
    project_mock.save.assert_called_once()


def test_build_vtpk_strips_doubled_main_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: arcpy.ListFeatureClasses() on a GeoPackage
    returns names already prefixed with 'main.' (the GPKG default
    schema). The previous build_vtpk code concatenated another
    'main.' on top, producing '<gpkg>/main.main.<layer>' which
    arcpy can't open ('Failed to add data. Possible credentials
    issue.' — Test 3's actual failure)."""
    cache = tmp_path / ".y2y"
    gpkg = tmp_path / "src.gpkg"
    gpkg.write_bytes(b"fake-gpkg")

    fake = types.ModuleType("arcpy")
    fake.env = types.SimpleNamespace(workspace=None)
    # ListFeatureClasses returns the 'main.'-prefixed name (as it
    # genuinely does on a GeoPackage workspace).
    fake.ListFeatureClasses = MagicMock(return_value=["main.parks_alberta"])

    captured_layer_ref: list[str] = []
    def _fake_create(in_map, output_file, **_):
        Path(output_file).write_bytes(b"fake-vtpk")
    fake.management = types.SimpleNamespace(
        CreateVectorTilePackage=_fake_create,
    )
    project_mock, map_mock = _stub_pro_project_plumbing(fake, tmp_path)
    map_mock.addDataFromPath = MagicMock(
        side_effect=lambda ref: captured_layer_ref.append(ref),
    )
    _install_fake_arcpy(monkeypatch, fake)

    agol_vtpk.build_vtpk(
        gpkg_path=gpkg, dataset_id="ds_alberta",
        checksum="hash_v1", cache_dir=cache,
    )

    assert len(captured_layer_ref) == 1
    ref = captured_layer_ref[0]
    # The right form is '<gpkg>\main.parks_alberta' — exactly ONE
    # 'main.' prefix.
    assert ref == f"{gpkg}\\main.parks_alberta", (
        f"layer_ref was {ref!r} — expected exactly one 'main.' prefix"
    )
    # Most importantly: NEVER doubled.
    assert "main.main." not in ref


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
