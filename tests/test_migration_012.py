"""Tests for migration 012 (format/crs -> library_format/library_crs)."""

from __future__ import annotations

import importlib
import sqlite3
from contextlib import closing

from pipeline import db as _db
from pipeline import inventory_manager

m011 = importlib.import_module("pipeline.migrations.011_authoritative_column")
m012 = importlib.import_module("pipeline.migrations.012_rename_format_crs_to_library")


def _columns(db_path) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {r[1] for r in conn.execute("PRAGMA table_info(datasets)")}


def _versions(db_path) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}


def _downgrade(db_path, *, also_drop_authoritative: bool = False) -> None:
    """Put the catalogue back into its pre-012 (optionally pre-011) shape."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("ALTER TABLE datasets RENAME COLUMN library_format TO format")
        conn.execute("ALTER TABLE datasets RENAME COLUMN library_crs TO crs")
        if also_drop_authoritative:
            conn.execute("ALTER TABLE datasets DROP COLUMN authoritative")
        conn.commit()


def test_renames_columns_and_keeps_values(project_tree, populate_dataset) -> None:
    dataset_id, _ = populate_dataset()
    db_path = project_tree["db"]
    _downgrade(db_path)
    assert {"format", "crs"} <= _columns(db_path)

    assert m012.run(db_path=db_path) == 0

    cols = _columns(db_path)
    assert {"library_format", "library_crs"} <= cols
    assert not {"format", "crs"} & cols
    assert "012" in _versions(db_path)

    row = inventory_manager.get_dataset(db_path, dataset_id)
    assert row["library_format"] == "geopackage"
    assert row["library_crs"] == "ESRI:102008"

    # The CHECK constraint followed the rename.
    with closing(sqlite3.connect(db_path)) as conn:
        try:
            conn.execute("UPDATE datasets SET library_format = 'shapefile'")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("CHECK on library_format not enforced")


def test_pre_0_9_0_catalogue_takes_011_and_012_in_either_order(
    project_tree, populate_dataset,
) -> None:
    dataset_id, _ = populate_dataset()
    db_path = project_tree["db"]
    _downgrade(db_path, also_drop_authoritative=True)

    assert m012.run(db_path=db_path) == 0
    assert m011.run(db_path=db_path) == 0

    row = inventory_manager.get_dataset(db_path, dataset_id)
    assert row["library_crs"] == "ESRI:102008"
    assert row["authoritative"] == 1


def test_fresh_catalogue_only_records_the_migration(tmp_path) -> None:
    db_path = tmp_path / "inventory.db"
    _db.get_connection(db_path).close()
    assert m012.run(db_path=db_path) == 0
    assert "012" in _versions(db_path)
    assert m012.run(db_path=db_path) == 1  # refuses a second run


def test_half_renamed_catalogue_is_refused(tmp_path) -> None:
    db_path = tmp_path / "inventory.db"
    _db.get_connection(db_path).close()
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("ALTER TABLE datasets RENAME COLUMN library_crs TO crs")
        conn.commit()
    assert m012.run(db_path=db_path) == 1
    assert "012" not in _versions(db_path)
