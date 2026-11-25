"""Tests for migration 011 (add datasets.authoritative)."""

from __future__ import annotations

import importlib
import sqlite3
from contextlib import closing

from pipeline import db as _db
from pipeline import inventory_manager

m011 = importlib.import_module("pipeline.migrations.011_authoritative_column")


def _columns(db_path) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {r[1] for r in conn.execute("PRAGMA table_info(datasets)")}


def _versions(db_path) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}


def test_adds_column_to_pre_011_catalogue(project_tree, populate_dataset) -> None:
    dataset_id, _ = populate_dataset()
    db_path = project_tree["db"]

    # Put the catalogue back into its pre-011 shape.
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("ALTER TABLE datasets DROP COLUMN authoritative")
        conn.commit()
    assert "authoritative" not in _columns(db_path)

    assert m011.run(db_path=db_path) == 0

    assert "authoritative" in _columns(db_path)
    assert "011" in _versions(db_path)
    # Existing rows become yes, and the catalogue reads normally again.
    assert inventory_manager.get_dataset(db_path, dataset_id)["authoritative"] == 1

    # The CHECK constraint came along.
    with closing(sqlite3.connect(db_path)) as conn:
        try:
            conn.execute("UPDATE datasets SET authoritative = 2")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("CHECK (authoritative IN (0, 1)) not enforced")


def test_fresh_catalogue_only_records_the_migration(tmp_path) -> None:
    db_path = tmp_path / "inventory.db"
    _db.get_connection(db_path).close()
    assert "authoritative" in _columns(db_path)

    assert m011.run(db_path=db_path) == 0
    assert "011" in _versions(db_path)


def test_refuses_to_run_twice(tmp_path) -> None:
    db_path = tmp_path / "inventory.db"
    _db.get_connection(db_path).close()
    assert m011.run(db_path=db_path) == 0
    assert m011.run(db_path=db_path) == 1


def test_missing_catalogue_is_an_error(tmp_path) -> None:
    assert m011.run(db_path=tmp_path / "nope.db") == 1
