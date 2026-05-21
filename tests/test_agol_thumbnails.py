"""Tests for pipeline.agol_thumbnails.

Uses the existing valid_gpkg_factory and valid_cog_factory fixtures
from conftest.py to build real source files at runtime, then exercises
generate_thumbnail() against them. The renderer reads from disk and
writes to disk; the unit under test is the cache logic + the
classification → renderer dispatch + the basic PNG validity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import agol_thumbnails


def _row(
    *,
    dataset_id: str = "ds_test",
    file_path: str = "test.gpkg",
    classification: str = "vector",
    checksum: str = "abc123",
) -> dict:
    return {
        "dataset_id": dataset_id,
        "file_path": file_path,
        "classification": classification,
        "checksum_sha256": checksum,
    }


# --- vector rendering --------------------------------------------------

def test_generate_thumbnail_for_vector_writes_png(
    tmp_path: Path, valid_gpkg_factory,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    valid_gpkg_factory("v.gpkg", dest_dir=library)
    row = _row(file_path="v.gpkg", classification="vector")

    cache = tmp_path / ".y2y"
    out = agol_thumbnails.generate_thumbnail(row, library, cache)

    assert out.exists()
    assert out.suffix == ".png"
    # PNG signature is the first 8 bytes
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


# --- raster rendering --------------------------------------------------

def test_generate_thumbnail_for_continuous_raster(
    tmp_path: Path, valid_cog_factory,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    valid_cog_factory("r.tif", dest_dir=library, dtype="float32", nodata=-9999.0)
    row = _row(file_path="r.tif", classification="continuous")

    cache = tmp_path / ".y2y"
    out = agol_thumbnails.generate_thumbnail(row, library, cache)

    assert out.exists()
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_generate_thumbnail_for_categorical_raster(
    tmp_path: Path, valid_cog_factory,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    valid_cog_factory("r.tif", dest_dir=library, dtype="uint8", nodata=255)
    row = _row(file_path="r.tif", classification="categorical")

    cache = tmp_path / ".y2y"
    out = agol_thumbnails.generate_thumbnail(row, library, cache)

    assert out.exists()
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


# --- caching -----------------------------------------------------------

def test_generate_thumbnail_cache_hit_skips_rerender(
    tmp_path: Path, valid_gpkg_factory,
) -> None:
    """Same checksum + cached PNG → return cached path without re-rendering."""
    library = tmp_path / "library"
    library.mkdir()
    valid_gpkg_factory("v.gpkg", dest_dir=library)
    row = _row(file_path="v.gpkg", classification="vector", checksum="cs1")

    cache = tmp_path / ".y2y"
    first = agol_thumbnails.generate_thumbnail(row, library, cache)
    first_mtime = first.stat().st_mtime_ns

    # Run again with the same checksum — should not re-render.
    second = agol_thumbnails.generate_thumbnail(row, library, cache)
    assert second == first
    assert second.stat().st_mtime_ns == first_mtime


def test_generate_thumbnail_cache_invalidates_on_checksum_change(
    tmp_path: Path, valid_gpkg_factory,
) -> None:
    """Different checksum → re-renders (sidecar guards this)."""
    library = tmp_path / "library"
    library.mkdir()
    valid_gpkg_factory("v.gpkg", dest_dir=library)
    cache = tmp_path / ".y2y"

    # First render with checksum 'cs1'.
    row_v1 = _row(file_path="v.gpkg", classification="vector", checksum="cs1")
    first = agol_thumbnails.generate_thumbnail(row_v1, library, cache)
    first_mtime = first.stat().st_mtime_ns

    # Sleep-free way to detect rerender: change checksum + verify the
    # sidecar updates.
    sidecar = first.with_suffix(first.suffix + ".sha256")
    assert sidecar.read_text(encoding="utf-8") == "cs1"

    row_v2 = _row(file_path="v.gpkg", classification="vector", checksum="cs2_new")
    second = agol_thumbnails.generate_thumbnail(row_v2, library, cache)
    assert sidecar.read_text(encoding="utf-8") == "cs2_new"
    assert second == first  # same path, different content + sidecar


# --- error paths -------------------------------------------------------

def test_generate_thumbnail_rejects_missing_dataset_id(tmp_path: Path) -> None:
    row = _row()
    row["dataset_id"] = None
    with pytest.raises(agol_thumbnails.ThumbnailError, match="no dataset_id"):
        agol_thumbnails.generate_thumbnail(row, tmp_path, tmp_path / ".y2y")


def test_generate_thumbnail_rejects_missing_file_path(tmp_path: Path) -> None:
    row = _row()
    row["file_path"] = None
    with pytest.raises(agol_thumbnails.ThumbnailError, match="no file_path"):
        agol_thumbnails.generate_thumbnail(row, tmp_path, tmp_path / ".y2y")


def test_generate_thumbnail_rejects_missing_source_file(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    row = _row(file_path="not_there.gpkg")
    with pytest.raises(agol_thumbnails.ThumbnailError, match="not found"):
        agol_thumbnails.generate_thumbnail(row, library, tmp_path / ".y2y")


def test_generate_thumbnail_rejects_unknown_classification(
    tmp_path: Path, valid_gpkg_factory,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    valid_gpkg_factory("v.gpkg", dest_dir=library)
    row = _row(file_path="v.gpkg", classification="bogus")
    with pytest.raises(agol_thumbnails.ThumbnailError, match="unsupported classification"):
        agol_thumbnails.generate_thumbnail(row, library, tmp_path / ".y2y")
