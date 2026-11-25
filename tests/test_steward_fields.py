"""Tests for steward-typed values: tags, authoritative, temporal coverage."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from pipeline import steward_fields as sf

# --- tags ---------------------------------------------------------------

def test_tags_split_on_semicolons_and_commas() -> None:
    assert sf.parse_tags("streams; test ;y2y") == ["streams", "test", "y2y"]
    assert sf.parse_tags("streams, test,y2y") == ["streams", "test", "y2y"]
    assert sf.normalize_tags("grizzly bear, habitat; Alberta") == "grizzly bear;habitat;Alberta"


def test_tags_drop_duplicates_ignoring_case() -> None:
    assert sf.parse_tags("Water;water; WATER;rivers") == ["Water", "rivers"]


@pytest.mark.parametrize("marker", ["NA", "na", "Na", "nA", " n/a ", "N/A", "NA;", "na ; "])
def test_no_tags_marker_in_any_case(marker: str) -> None:
    assert sf.parse_tags(marker) == []
    assert sf.normalize_tags(marker) == "NA"
    assert sf.tags_for_agol(marker) == []


@pytest.mark.parametrize("value", [None, "", "   ", ";;", " , "])
def test_empty_tags_are_rejected(value) -> None:
    with pytest.raises(ValueError, match="enter NA"):
        sf.parse_tags(value)


@pytest.mark.parametrize("value", ["NA;streams", "streams, na", "rivers;N/A;lakes"])
def test_marker_mixed_with_real_tags_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="mixes"):
        sf.parse_tags(value)


@pytest.mark.parametrize("value", ["Canada", "national parks", "banana", "DNA", "nature"])
def test_tags_that_only_contain_na_are_real_tags(value: str) -> None:
    assert sf.parse_tags(value) == [value]
    assert sf.tags_for_agol(value) == [value]


def test_tags_for_agol_never_publishes_the_marker() -> None:
    assert sf.tags_for_agol(None) == []
    assert sf.tags_for_agol("") == []
    assert sf.tags_for_agol(["rivers", "na"]) == ["rivers"]
    assert sf.tags_for_agol("rivers;lakes") == ["rivers", "lakes"]


# --- authoritative ------------------------------------------------------

@pytest.mark.parametrize("value", ["yes", "Yes", " YES ", "y", "true", True, 1, "1"])
def test_authoritative_yes(value) -> None:
    assert sf.parse_authoritative(value) == 1


@pytest.mark.parametrize("value", ["no", "No", "n", "false", False, 0, "0"])
def test_authoritative_no(value) -> None:
    assert sf.parse_authoritative(value) == 0


@pytest.mark.parametrize("value", [None, "", "maybe", 2, "NA"])
def test_authoritative_rejects_anything_else(value) -> None:
    with pytest.raises(ValueError, match="'yes' or 'no'"):
        sf.parse_authoritative(value)


def test_format_authoritative() -> None:
    assert sf.format_authoritative(1) == "yes"
    assert sf.format_authoritative(0) == "no"
    assert sf.format_authoritative(None) is None


# --- temporal coverage --------------------------------------------------

def test_temporal_accepts_year_month_and_date() -> None:
    assert sf.parse_temporal("2020") == "2020-01-01T00:00:00Z"
    assert sf.parse_temporal(2020) == "2020-01-01T00:00:00Z"
    assert sf.parse_temporal(2020.0) == "2020-01-01T00:00:00Z"
    assert sf.parse_temporal("2020", end=True) == "2020-12-31T00:00:00Z"
    assert sf.parse_temporal("2020-02", end=True) == "2020-02-29T00:00:00Z"
    assert sf.parse_temporal("2020-06") == "2020-06-01T00:00:00Z"
    assert sf.parse_temporal("2020-6-5") == "2020-06-05T00:00:00Z"
    assert sf.parse_temporal("2020-06-15T00:00:00Z") == "2020-06-15T00:00:00Z"


def test_temporal_accepts_excel_date_cells() -> None:
    assert sf.parse_temporal(datetime(2023, 3, 30)) == "2023-03-30T00:00:00Z"
    assert sf.parse_temporal(date(2023, 3, 30)) == "2023-03-30T00:00:00Z"


def test_temporal_empty_is_none() -> None:
    assert sf.parse_temporal(None) is None
    assert sf.parse_temporal("  ") is None


@pytest.mark.parametrize("value", ["last year", "2020-13", "2020-02-30", "20", "June 2020"])
def test_temporal_rejects_non_dates(value: str) -> None:
    with pytest.raises(ValueError, match="YYYY"):
        sf.parse_temporal(value)


def test_temporal_end_before_start_is_rejected() -> None:
    sf.check_temporal_order("2020-01-01T00:00:00Z", "2020-12-31T00:00:00Z")
    sf.check_temporal_order(None, "2020-12-31T00:00:00Z")
    with pytest.raises(ValueError, match="before"):
        sf.check_temporal_order("2021-01-01T00:00:00Z", "2020-12-31T00:00:00Z")
