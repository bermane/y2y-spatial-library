"""Tests for the SQLite-backed inventory manager.

Exercises the public API of ``pipeline.inventory_manager`` directly,
without going through the ingest pipeline. Each test populates a
freshly-created ``inventory.db`` with rows that satisfy every NOT NULL
/ CHECK constraint declared in ``pipeline/schema.sql``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from pipeline import db as _db
from pipeline import inventory_manager as im


# A row that satisfies every NOT NULL + CHECK constraint in schema.sql.
# Tests that need a row override `dataset_id` (and any field they're
# specifically exercising) on top of this template.
def _row(dataset_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        # identity & taxonomy
        "dataset_id": dataset_id,
        "dataset_type": "spatial",
        "title": f"Title {dataset_id}",
        "category": "Water",
        "subcategory": None,
        "file_path": f"Water/{dataset_id}.gpkg",
        "format": "geopackage",
        # extrinsic
        "summary": "Summary.",
        "description": "Description.",
        "tags": "tag",
        "terms_of_use": "TOU.",
        "acknowledgements": "Ack.",
        "data_steward": "Tester",
        "internal_notes": None,
        # lifecycle
        "status": "active",
        "date_added": "2026-04-29T00:00:00Z",
        "date_modified": "2026-04-29T00:00:00Z",
        # AGOL
        "agol_item_id": None,
        "agol_published_at": None,
        "last_synced_at": None,
        "sync_status": "unpublished",
        # intrinsic snapshot
        "checksum_sha256": "0" * 64,
        "size_bytes": 1024,
        "mtime": "2026-04-29T00:00:00Z",
        "crs": "ESRI:102008",
        "geographic_extent_bbox": "0,0,1,1",
        "classification": "vector",
        # spatial properties
        "footprint_wkt": None,
        "temporal_start": None,
        "temporal_end": None,
        "feature_count": 1,
        "raster_width": None,
        "raster_height": None,
        "pixel_size_x": None,
        "pixel_size_y": None,
        # source provenance
        "source_format": "geopackage",
        "source_filename": f"{dataset_id}_source.gpkg",
        "source_crs": "ESRI:102008",
        "source_layer": None,
    }
    base.update(overrides)
    return base


# --- read API ----------------------------------------------------------

def test_load_returns_empty_when_db_missing(tmp_path: Path) -> None:
    """Pre-creation read returns an empty list, not an error."""
    assert im.load_inventory(tmp_path / "missing.db") == []


def test_load_returns_empty_for_freshly_created_db(tmp_path: Path) -> None:
    """Connecting auto-creates the schema; an empty db yields no rows."""
    db = tmp_path / "inv.db"
    with _db.connect(db):  # touch — applies schema
        pass
    assert im.load_inventory(db) == []


def test_get_dataset_returns_row(tmp_path: Path) -> None:
    db = tmp_path / "inv.db"
    im.insert_dataset(db, _row("ds_aaaaaaaaaaaaaaaaaaaaaaaaaa"))
    row = im.get_dataset(db, "ds_aaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert row is not None
    assert row["title"] == "Title ds_aaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_get_dataset_returns_none_when_missing(tmp_path: Path) -> None:
    db = tmp_path / "inv.db"
    im.insert_dataset(db, _row("ds_aaaaaaaaaaaaaaaaaaaaaaaaaa"))
    assert im.get_dataset(db, "ds_does_not_exist") is None


# --- write API: insert -------------------------------------------------

def test_insert_then_load_roundtrips(tmp_path: Path) -> None:
    db = tmp_path / "inv.db"
    im.insert_dataset(db, _row("ds_aaaaaaaaaaaaaaaaaaaaaaaaaa"))
    im.insert_dataset(db, _row("ds_bbbbbbbbbbbbbbbbbbbbbbbbbb"))

    loaded = im.load_inventory(db)
    assert {r["dataset_id"] for r in loaded} == {
        "ds_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "ds_bbbbbbbbbbbbbbbbbbbbbbbbbb",
    }
    assert all(r["dataset_type"] == "spatial" for r in loaded)


def test_insert_rejects_invalid_format_via_check_constraint(tmp_path: Path) -> None:
    db = tmp_path / "inv.db"
    bad = _row("ds_aaaaaaaaaaaaaaaaaaaaaaaaaa", format="GeoPackage")  # display-cased
    with pytest.raises(sqlite3.IntegrityError):
        im.insert_dataset(db, bad)


def test_insert_rejects_dataset_id_without_prefix(tmp_path: Path) -> None:
    db = tmp_path / "inv.db"
    with pytest.raises(sqlite3.IntegrityError):
        im.insert_dataset(db, _row("nope"))


def test_insert_rejects_null_in_required_column(tmp_path: Path) -> None:
    """STRICT + NOT NULL means the schema rejects missing requireds."""
    db = tmp_path / "inv.db"
    bad = _row("ds_aaaaaaaaaaaaaaaaaaaaaaaaaa")
    bad["title"] = None
    with pytest.raises(sqlite3.IntegrityError):
        im.insert_dataset(db, bad)


# --- write API: update -------------------------------------------------

def test_update_dataset_changes_field(tmp_path: Path) -> None:
    db = tmp_path / "inv.db"
    did = "ds_aaaaaaaaaaaaaaaaaaaaaaaaaa"
    im.insert_dataset(db, _row(did))
    im.update_dataset(db, did, {"summary": "Revised."})

    assert im.get_dataset(db, did)["summary"] == "Revised."


def test_update_dataset_rejects_unknown_column(tmp_path: Path) -> None:
    db = tmp_path / "inv.db"
    did = "ds_aaaaaaaaaaaaaaaaaaaaaaaaaa"
    im.insert_dataset(db, _row(did))
    with pytest.raises(ValueError, match="unknown columns"):
        im.update_dataset(db, did, {"made_up_field": 1})


def test_update_dataset_raises_keyerror_for_missing_id(tmp_path: Path) -> None:
    db = tmp_path / "inv.db"
    im.insert_dataset(db, _row("ds_aaaaaaaaaaaaaaaaaaaaaaaaaa"))
    with pytest.raises(KeyError):
        im.update_dataset(db, "ds_does_not_exist", {"summary": "x"})


def test_update_dataset_empty_fields_is_noop(tmp_path: Path) -> None:
    db = tmp_path / "inv.db"
    did = "ds_aaaaaaaaaaaaaaaaaaaaaaaaaa"
    im.insert_dataset(db, _row(did))
    im.update_dataset(db, did, {})  # no-op, no exception


# --- write API: append_inventory --------------------------------------

def test_append_skips_duplicate_dataset_ids(tmp_path: Path) -> None:
    db = tmp_path / "inv.db"
    im.insert_dataset(db, _row("ds_aaaaaaaaaaaaaaaaaaaaaaaaaa"))

    # First row's id collides; second row's is new.
    im.append_inventory(db, [
        _row("ds_aaaaaaaaaaaaaaaaaaaaaaaaaa", title="Different"),
        _row("ds_cccccccccccccccccccccccccc"),
    ])

    loaded = {r["dataset_id"]: r["title"] for r in im.load_inventory(db)}
    # Original title preserved (no UPDATE-on-conflict for append_inventory)
    assert loaded["ds_aaaaaaaaaaaaaaaaaaaaaaaaaa"] == "Title ds_aaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert loaded["ds_cccccccccccccccccccccccccc"] == "Title ds_cccccccccccccccccccccccccc"


def test_append_drops_unknown_columns_silently(tmp_path: Path) -> None:
    """A pending-sheet row carries extra columns (`ready`, `target_filename`,
    `_validation_error`) that aren't on the schema; insert_dataset/append
    must silently drop them rather than fail."""
    db = tmp_path / "inv.db"
    row = _row("ds_dddddddddddddddddddddddddd")
    row["ready"] = True
    row["_validation_error"] = "stale"
    row["target_filename"] = "x.gpkg"

    im.append_inventory(db, [row])
    loaded = im.load_inventory(db)[0]
    assert "ready" not in loaded
    assert "_validation_error" not in loaded
    assert "target_filename" not in loaded


# --- write API: save_inventory (UPSERT) -------------------------------

def test_save_inventory_upserts_existing_rows(tmp_path: Path) -> None:
    db = tmp_path / "inv.db"
    did = "ds_aaaaaaaaaaaaaaaaaaaaaaaaaa"
    im.insert_dataset(db, _row(did, title="Original"))

    im.save_inventory(db, [_row(did, title="Updated")])

    assert im.get_dataset(db, did)["title"] == "Updated"


def test_save_inventory_does_not_remove_absent_rows(tmp_path: Path) -> None:
    """save_inventory is UPSERT, not REPLACE-ALL — preserves rows that
    are foreign-keyed by changelog history."""
    db = tmp_path / "inv.db"
    a = "ds_aaaaaaaaaaaaaaaaaaaaaaaaaa"
    b = "ds_bbbbbbbbbbbbbbbbbbbbbbbbbb"
    im.insert_dataset(db, _row(a))
    im.insert_dataset(db, _row(b))

    # Save only `a` — `b` should still be present.
    im.save_inventory(db, [_row(a, title="A only")])

    ids = {r["dataset_id"] for r in im.load_inventory(db)}
    assert ids == {a, b}


# --- changelog --------------------------------------------------------

def test_append_changelog_writes_a_row(tmp_path: Path) -> None:
    db = tmp_path / "inv.db"
    did = "ds_aaaaaaaaaaaaaaaaaaaaaaaaaa"
    im.insert_dataset(db, _row(did))

    im.append_changelog(
        db,
        timestamp="2026-04-24T15:00:00Z",
        action="add",
        dataset_id=did,
        actor="Ethan",
        path="Water/streams_2024.gpkg",
        detail="Ingested 'Streams 2024' (version 1.0).",
    )

    log = im.load_changelog(db)
    assert len(log) == 1
    entry = log[0]
    assert entry["action"] == "add"
    assert entry["actor"] == "Ethan"
    assert entry["dataset_id"] == did
    # path is folded into note as a [path: ...] prefix
    assert "[path: Water/streams_2024.gpkg]" in entry["note"]
    assert "Ingested 'Streams 2024'" in entry["note"]


def test_append_changelog_records_structured_diff(tmp_path: Path) -> None:
    db = tmp_path / "inv.db"
    did = "ds_aaaaaaaaaaaaaaaaaaaaaaaaaa"
    im.insert_dataset(db, _row(did))

    im.append_changelog(
        db,
        timestamp="2026-04-24T15:00:00Z",
        action="update",
        dataset_id=did,
        actor="Ethan",
        path=None,
        detail="title updated",
        field_changed="title",
        old_value="Old",
        new_value="New",
    )

    entry = im.load_changelog(db)[0]
    assert entry["field_changed"] == "title"
    assert entry["old_value"] == "Old"
    assert entry["new_value"] == "New"


def test_append_changelog_rejects_unknown_action(tmp_path: Path) -> None:
    db = tmp_path / "inv.db"
    did = "ds_aaaaaaaaaaaaaaaaaaaaaaaaaa"
    im.insert_dataset(db, _row(did))

    with pytest.raises(ValueError, match="not allowed"):
        im.append_changelog(
            db,
            timestamp="2026-04-24T15:00:00Z",
            action="frobnicate",
            dataset_id=did,
            actor="Ethan",
            path=None,
            detail="x",
        )


def test_append_changelog_rejects_dangling_dataset_id(tmp_path: Path) -> None:
    """FK ON DELETE RESTRICT means an INSERT that points at a missing
    dataset is rejected at the SQL boundary, not silently orphaned."""
    db = tmp_path / "inv.db"
    with pytest.raises(ValueError, match="failed to append"):
        im.append_changelog(
            db,
            timestamp="2026-04-24T15:00:00Z",
            action="add",
            dataset_id="ds_does_not_exist",
            actor="Ethan",
            path=None,
            detail="x",
        )


def test_changelog_is_append_only_and_orders_by_timestamp(tmp_path: Path) -> None:
    db = tmp_path / "inv.db"
    did = "ds_aaaaaaaaaaaaaaaaaaaaaaaaaa"
    im.insert_dataset(db, _row(did))
    im.append_changelog(
        db, timestamp="2026-04-24T15:00:00Z", action="add",
        dataset_id=did, actor="Ethan", path=None, detail="first",
    )
    im.append_changelog(
        db, timestamp="2026-04-24T16:00:00Z", action="update",
        dataset_id=did, actor="Ethan", path=None, detail="second",
    )

    log = im.load_changelog(db)
    assert len(log) == 2
    assert "first" in log[0]["note"]
    assert "second" in log[1]["note"]
    # Each row gets a fresh cl_<ULID> id
    assert log[0]["id"] != log[1]["id"]
    assert all(r["id"].startswith("cl_") for r in log)


# --- xlsx-lock detection (export-only) --------------------------------

def test_assert_not_locked_passes_when_no_lock_file(tmp_path: Path) -> None:
    p = tmp_path / "inventory.xlsx"
    p.write_bytes(b"")
    im.assert_not_locked(p)  # no exception


def test_assert_not_locked_raises_when_lock_file_present(tmp_path: Path) -> None:
    p = tmp_path / "inventory.xlsx"
    p.write_bytes(b"")
    (tmp_path / "~$inventory.xlsx").write_bytes(b"")  # Excel-style lock
    with pytest.raises(im.InventoryLockedError, match="open in Excel"):
        im.assert_not_locked(p)
