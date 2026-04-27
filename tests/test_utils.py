"""Pure-function tests for pipeline.utils (no GIS deps required)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pipeline import utils


def test_sha256_file_is_stable_and_correct(tmp_path: Path) -> None:
    p = tmp_path / "hello.bin"
    p.write_bytes(b"hello world")

    first = utils.sha256_file(p)
    second = utils.sha256_file(p)

    assert first == second
    assert first == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


def test_stat_signature_reports_real_size(tmp_path: Path) -> None:
    p = tmp_path / "x.bin"
    p.write_bytes(b"abcdef")

    size, mtime = utils.stat_signature(p)

    assert size == 6
    assert mtime.endswith("Z") and "T" in mtime


def test_new_dataset_id_is_prefixed_and_unique() -> None:
    ids = {utils.new_dataset_id() for _ in range(200)}

    assert len(ids) == 200
    assert all(i.startswith("ds_") and len(i) == 15 for i in ids)


def test_slugify_title_handles_typical_cases() -> None:
    assert utils.slugify_title("Grizzly Bear Den Sites 2024") == "grizzly_bear_den_sites_2024"
    assert utils.slugify_title("Roads & Railways") == "roads_railways"
    assert utils.slugify_title("  Spaced  Out  ") == "spaced_out"


def test_slugify_title_splits_camel_case() -> None:
    assert utils.slugify_title("Y2Y_RegionBoundary") == "y2y_region_boundary"
    assert utils.slugify_title("MyDataset") == "my_dataset"
    assert utils.slugify_title("XMLParser") == "xml_parser"
    assert utils.slugify_title("BurnSeverity2023LandsatComposite") == (
        "burn_severity2023_landsat_composite"
    )


def test_slugify_title_preserves_acronyms_with_digits() -> None:
    """Y2Y, NAD83, etc. should not get split at digit→letter boundaries."""
    assert utils.slugify_title("Y2Y") == "y2y"
    assert utils.slugify_title("NAD83Albers") == "nad83_albers"


def test_slugify_title_collapses_repeated_underscores() -> None:
    assert utils.slugify_title("foo__bar___baz") == "foo_bar_baz"
    assert utils.slugify_title("_leading_") == "leading"


def test_titleify_slug_round_trips_basic_case() -> None:
    assert utils.titleify_slug("grizzly_bear_den_sites") == "Grizzly Bear Den Sites"


def test_utc_now_iso_is_parseable_iso8601() -> None:
    stamp = utils.utc_now_iso()
    # Replace the trailing Z so fromisoformat accepts it on 3.11
    datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def test_is_vector_and_is_raster_classify_by_extension() -> None:
    assert utils.is_vector(Path("foo.gpkg"))
    assert not utils.is_vector(Path("foo.tif"))
    assert utils.is_raster(Path("foo.tif"))
    assert utils.is_raster(Path("foo.TIFF"))
    assert not utils.is_raster(Path("foo.gpkg"))


def test_bbox_to_string_formats_six_decimals() -> None:
    assert utils.bbox_to_string((1.0, 2.0, 3.0, 4.0)) == "1.000000,2.000000,3.000000,4.000000"
