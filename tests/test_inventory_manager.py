"""Tests for inventory.xlsx round-trip and changelog append-only behaviour."""

from __future__ import annotations

from pathlib import Path

from pipeline import inventory_manager as im


def _row(dataset_id: str, **overrides: object) -> dict:
    base = {name: None for name in im.INVENTORY_COLUMN_NAMES}
    base["dataset_id"] = dataset_id
    base["title"] = f"Title {dataset_id}"
    base.update(overrides)
    return base


def test_load_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert im.load_inventory(tmp_path / "missing.xlsx") == []


def test_save_then_load_roundtrips(tmp_path: Path) -> None:
    path = tmp_path / "inventory.xlsx"
    rows = [_row("ds_aaaaaaaaaaaa"), _row("ds_bbbbbbbbbbbb")]
    im.save_inventory(path, rows)

    loaded = im.load_inventory(path)
    assert {r["dataset_id"] for r in loaded} == {"ds_aaaaaaaaaaaa", "ds_bbbbbbbbbbbb"}
    assert all("title" in r for r in loaded)


def test_append_skips_duplicate_dataset_ids(tmp_path: Path) -> None:
    path = tmp_path / "inventory.xlsx"
    im.save_inventory(path, [_row("ds_aaaaaaaaaaaa")])

    # Same id should be skipped; new id should land
    im.append_inventory(path, [_row("ds_aaaaaaaaaaaa", title="Different"),
                               _row("ds_cccccccccccc")])

    loaded = im.load_inventory(path)
    titles_by_id = {r["dataset_id"]: r["title"] for r in loaded}
    assert titles_by_id["ds_aaaaaaaaaaaa"] == "Title ds_aaaaaaaaaaaa"  # unchanged
    assert "ds_cccccccccccc" in titles_by_id


def test_append_strips_non_canonical_columns(tmp_path: Path) -> None:
    path = tmp_path / "inventory.xlsx"
    row = _row("ds_dddddddddddd")
    row["ready"] = True  # pending-only column should not survive
    row["_validation_error"] = "stale"

    im.append_inventory(path, [row])

    loaded = im.load_inventory(path)
    assert "ready" not in loaded[0]
    assert "_validation_error" not in loaded[0]


def test_changelog_appends_and_creates_file(tmp_path: Path) -> None:
    log = tmp_path / "changelog.md"
    im.append_changelog(
        log,
        timestamp="2026-04-24T15:00:00Z",
        action="add",
        dataset_id="ds_aaaaaaaaaaaa",
        actor="Ethan",
        path="Water/streams_2024.gpkg",
        detail="Ingested 'Streams 2024' (version 1.0).",
    )
    text = log.read_text()
    assert "## 2026-04-24T15:00:00Z — add — ds_aaaaaaaaaaaa" in text
    assert "actor:  Ethan" in text
    assert "path:   Water/streams_2024.gpkg" in text
    assert "Ingested 'Streams 2024'" in text


def test_changelog_is_append_only(tmp_path: Path) -> None:
    log = tmp_path / "changelog.md"
    im.append_changelog(
        log, timestamp="2026-04-24T15:00:00Z", action="add",
        dataset_id="ds_aaaaaaaaaaaa", actor="Ethan", path="x.gpkg", detail="first",
    )
    im.append_changelog(
        log, timestamp="2026-04-24T16:00:00Z", action="add",
        dataset_id="ds_bbbbbbbbbbbb", actor="Ethan", path="y.gpkg", detail="second",
    )
    text = log.read_text()
    # First entry must still be present
    assert "ds_aaaaaaaaaaaa" in text
    assert "ds_bbbbbbbbbbbb" in text
    # Order is preserved (older above newer)
    assert text.index("first") < text.index("second")
