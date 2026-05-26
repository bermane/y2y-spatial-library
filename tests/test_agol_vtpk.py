"""Tests for pipeline.agol_vtpk (rev 3 — manual VTPK + queue ingest).

The arcpy-driven path tested here in earlier revisions has been
retired (it proved fragile across ArcGIS Pro upgrades). These
tests cover the new responsibilities:

* path resolution (catalogue row → ``library/vtpk/<stem>.vtpk``)
* presence + staleness probes used by push pre-flight + reconcile
* the per-file ingest flow that ``y2y ingest scan`` dispatches for
  every ``.vtpk`` discovered in ``queue/incoming/``
* the SHA-256 sidecar pattern (cache the hash, recompute if missing)
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from pipeline import agol_vtpk, inventory_manager


# A minimal valid VTPK starts with the ZIP local file header magic.
# agol_vtpk.ingest_one_vtpk sniffs the first four bytes; the rest
# of the file is opaque to it (AGOL handles unpacking, not us).
_ZIP_MAGIC = b"PK\x03\x04"


# ----------------------------------------------------------------------------
# Path resolution + probes
# ----------------------------------------------------------------------------

def test_resolve_vtpk_path_for_typical_row(tmp_path: Path) -> None:
    """A row's GPKG at library/spatial/<Category>/<stem>.gpkg maps
    to library/vtpk/<stem>.vtpk."""
    library_root = tmp_path / "library" / "spatial"
    library_root.mkdir(parents=True)
    row = {
        "dataset_id": "ds_X",
        "file_path": "Land_Designations_Tenure/parks_protected_areas_alberta.gpkg",
    }
    out = agol_vtpk.resolve_vtpk_path(row, library_root)
    assert out == library_root.parent / "vtpk" / "parks_protected_areas_alberta.vtpk"


def test_resolve_vtpk_path_raises_when_file_path_missing(tmp_path: Path) -> None:
    library_root = tmp_path / "library" / "spatial"
    library_root.mkdir(parents=True)
    with pytest.raises(ValueError, match="no file_path"):
        agol_vtpk.resolve_vtpk_path({"dataset_id": "ds_X"}, library_root)


def test_vtpk_present_false_when_missing(tmp_path: Path) -> None:
    library_root = tmp_path / "library" / "spatial"
    library_root.mkdir(parents=True)
    row = {"dataset_id": "ds_X", "file_path": "Water/v.gpkg"}
    assert agol_vtpk.vtpk_present(row, library_root) is False


def test_vtpk_present_true_when_file_exists(tmp_path: Path) -> None:
    library_root = tmp_path / "library" / "spatial"
    library_root.mkdir(parents=True)
    vtpk_dir = library_root.parent / "vtpk"
    vtpk_dir.mkdir()
    (vtpk_dir / "v.vtpk").write_bytes(_ZIP_MAGIC + b"payload")
    row = {"dataset_id": "ds_X", "file_path": "Water/v.gpkg"}
    assert agol_vtpk.vtpk_present(row, library_root) is True


def test_vtpk_stale_when_gpkg_newer_than_vtpk(tmp_path: Path) -> None:
    library_root = tmp_path / "library" / "spatial"
    (library_root / "Water").mkdir(parents=True)
    gpkg = library_root / "Water" / "v.gpkg"
    gpkg.write_bytes(b"gpkg-bytes")

    vtpk_dir = library_root.parent / "vtpk"
    vtpk_dir.mkdir()
    vtpk = vtpk_dir / "v.vtpk"
    vtpk.write_bytes(_ZIP_MAGIC + b"payload")

    # Set the GPKG mtime to be 60 seconds NEWER than the VTPK.
    vtpk_mtime = vtpk.stat().st_mtime
    new_gpkg_mtime = vtpk_mtime + 60
    os.utime(gpkg, (new_gpkg_mtime, new_gpkg_mtime))

    row = {"dataset_id": "ds_X", "file_path": "Water/v.gpkg"}
    assert agol_vtpk.vtpk_stale(row, library_root) is True


def test_vtpk_stale_false_when_vtpk_newer(tmp_path: Path) -> None:
    library_root = tmp_path / "library" / "spatial"
    (library_root / "Water").mkdir(parents=True)
    gpkg = library_root / "Water" / "v.gpkg"
    gpkg.write_bytes(b"gpkg-bytes")
    # Sleep a tick so the VTPK we write next is genuinely newer.
    time.sleep(0.01)
    vtpk_dir = library_root.parent / "vtpk"
    vtpk_dir.mkdir()
    (vtpk_dir / "v.vtpk").write_bytes(_ZIP_MAGIC + b"payload")

    row = {"dataset_id": "ds_X", "file_path": "Water/v.gpkg"}
    assert agol_vtpk.vtpk_stale(row, library_root) is False


def test_vtpk_stale_false_when_vtpk_missing(tmp_path: Path) -> None:
    """vtpk_stale should return False (not True) when the VTPK
    doesn't exist — the missing case is handled by vtpk_present."""
    library_root = tmp_path / "library" / "spatial"
    (library_root / "Water").mkdir(parents=True)
    (library_root / "Water" / "v.gpkg").write_bytes(b"gpkg")
    row = {"dataset_id": "ds_X", "file_path": "Water/v.gpkg"}
    assert agol_vtpk.vtpk_stale(row, library_root) is False


# ----------------------------------------------------------------------------
# Sidecar checksum caching
# ----------------------------------------------------------------------------

def test_read_vtpk_checksum_writes_sidecar_first_call(tmp_path: Path) -> None:
    vtpk = tmp_path / "v.vtpk"
    vtpk.write_bytes(_ZIP_MAGIC + b"abc")
    digest = agol_vtpk.read_vtpk_checksum(vtpk)
    assert len(digest) == 64  # hex sha256
    sidecar = vtpk.with_suffix(".vtpk.sha256")
    assert sidecar.exists()
    assert sidecar.read_text() == digest


def test_read_vtpk_checksum_reads_from_sidecar_subsequent_calls(
    tmp_path: Path,
) -> None:
    """When the sidecar already records a hash, we trust it (the
    file may be huge; re-streaming on every push is wasteful)."""
    vtpk = tmp_path / "v.vtpk"
    vtpk.write_bytes(_ZIP_MAGIC + b"abc")
    sidecar = vtpk.with_suffix(".vtpk.sha256")
    sidecar.write_text("preset_hash_from_disk")
    assert agol_vtpk.read_vtpk_checksum(vtpk) == "preset_hash_from_disk"


# ----------------------------------------------------------------------------
# Ingest happy + sad paths
# ----------------------------------------------------------------------------

def _setup_catalogue_row(
    db_path: Path, *, agol_format: str, file_stem: str = "parks",
) -> str:
    """Insert one catalogue row whose file_path is
    ``Land_Designations_Tenure/<stem>.gpkg`` with the given
    ``agol_format``. Returns the dataset_id. Reuses
    test_agol_push._full_row to stay in sync with the schema's
    NOT NULL set as it evolves."""
    from tests.test_agol_push import _full_row
    row = _full_row(
        dataset_id="ds_test_vtl",
        file_path=f"Land_Designations_Tenure/{file_stem}.gpkg",
        category="Land Designations & Tenure",
        agol_format=agol_format,
    )
    inventory_manager.insert_dataset(db_path, row)
    return row["dataset_id"]


def test_ingest_one_vtpk_happy_path(
    project_tree, valid_gpkg_factory,
) -> None:
    """Single matched .vtpk in queue → moved to library/vtpk/,
    sidecar written, changelog entry appended."""
    db_path = project_tree["db"]
    library_root = project_tree["library"]
    valid_gpkg_factory("parks.gpkg", dest_dir=library_root / "Land_Designations_Tenure")
    _setup_catalogue_row(db_path, agol_format="vector-tile-layer")

    queue_incoming = project_tree["root"] / "queue" / "incoming"
    queue_incoming.mkdir(parents=True, exist_ok=True)
    vtpk_in_queue = queue_incoming / "parks.vtpk"
    vtpk_in_queue.write_bytes(_ZIP_MAGIC + b"payload-bytes")

    result = agol_vtpk.ingest_one_vtpk(
        vtpk_in_queue, library_root, db_path, actor="tester",
    )

    assert result.status == "moved"
    assert result.dataset_id == "ds_test_vtl"
    assert result.destination is not None
    assert result.destination.exists()
    assert result.destination == library_root.parent / "vtpk" / "parks.vtpk"
    # Source removed from queue.
    assert not vtpk_in_queue.exists()
    # Sidecar created.
    assert result.destination.with_suffix(".vtpk.sha256").exists()


def test_ingest_one_vtpk_rejects_invalid_zip_signature(
    project_tree, valid_gpkg_factory,
) -> None:
    """Files not starting with PK\\x03\\x04 are rejected without
    touching the catalogue or moving anything."""
    db_path = project_tree["db"]
    library_root = project_tree["library"]
    valid_gpkg_factory("parks.gpkg", dest_dir=library_root / "Land_Designations_Tenure")
    _setup_catalogue_row(db_path, agol_format="vector-tile-layer")

    queue_incoming = project_tree["root"] / "queue" / "incoming"
    queue_incoming.mkdir(parents=True, exist_ok=True)
    bogus = queue_incoming / "parks.vtpk"
    bogus.write_bytes(b"NOT-A-ZIP-FILE")

    result = agol_vtpk.ingest_one_vtpk(
        bogus, library_root, db_path, actor="tester",
    )

    assert result.status == "invalid"
    assert "ZIP signature" in result.message
    # File stayed in queue.
    assert bogus.exists()


def test_ingest_one_vtpk_unmatched_when_no_row(
    project_tree,
) -> None:
    """A .vtpk whose stem doesn't match any active catalogue row
    is reported as unmatched, left in the queue."""
    db_path = project_tree["db"]
    library_root = project_tree["library"]

    queue_incoming = project_tree["root"] / "queue" / "incoming"
    queue_incoming.mkdir(parents=True, exist_ok=True)
    orphan = queue_incoming / "no_matching_row.vtpk"
    orphan.write_bytes(_ZIP_MAGIC + b"payload")

    result = agol_vtpk.ingest_one_vtpk(
        orphan, library_root, db_path, actor="tester",
    )

    assert result.status == "unmatched"
    assert "no active catalogue row" in result.message
    assert orphan.exists()  # not moved


def test_ingest_one_vtpk_target_mismatch_rejects(
    project_tree, valid_gpkg_factory,
) -> None:
    """A .vtpk whose matched row has agol_format='feature-layer'
    is rejected with an actionable message — the steward needs to
    flip the target first via `y2y update`."""
    db_path = project_tree["db"]
    library_root = project_tree["library"]
    valid_gpkg_factory("parks.gpkg", dest_dir=library_root / "Land_Designations_Tenure")
    _setup_catalogue_row(db_path, agol_format="feature-layer")

    queue_incoming = project_tree["root"] / "queue" / "incoming"
    queue_incoming.mkdir(parents=True, exist_ok=True)
    vtpk = queue_incoming / "parks.vtpk"
    vtpk.write_bytes(_ZIP_MAGIC + b"payload")

    result = agol_vtpk.ingest_one_vtpk(
        vtpk, library_root, db_path, actor="tester",
    )

    assert result.status == "target_mismatch"
    assert result.dataset_id == "ds_test_vtl"
    assert "agol_format" in result.message
    assert "vector-tile-layer" in result.message
    assert vtpk.exists()  # not moved


def test_ingest_one_vtpk_replaces_existing_at_destination(
    project_tree, valid_gpkg_factory,
) -> None:
    """A subsequent re-ingest of the same row's VTPK replaces the
    prior one at library/vtpk/, updates the sidecar checksum."""
    db_path = project_tree["db"]
    library_root = project_tree["library"]
    valid_gpkg_factory("parks.gpkg", dest_dir=library_root / "Land_Designations_Tenure")
    _setup_catalogue_row(db_path, agol_format="vector-tile-layer")

    # Plant an old VTPK at the canonical location.
    vtpk_dir = library_root.parent / "vtpk"
    vtpk_dir.mkdir(parents=True, exist_ok=True)
    old_vtpk = vtpk_dir / "parks.vtpk"
    old_vtpk.write_bytes(_ZIP_MAGIC + b"OLD-PAYLOAD")
    old_sidecar = old_vtpk.with_suffix(".vtpk.sha256")
    old_sidecar.write_text("old_hash")

    # New VTPK in queue.
    queue_incoming = project_tree["root"] / "queue" / "incoming"
    queue_incoming.mkdir(parents=True, exist_ok=True)
    new_vtpk = queue_incoming / "parks.vtpk"
    new_vtpk.write_bytes(_ZIP_MAGIC + b"NEW-PAYLOAD")

    result = agol_vtpk.ingest_one_vtpk(
        new_vtpk, library_root, db_path, actor="tester",
    )

    assert result.status == "moved"
    assert result.destination is not None
    # Destination now has the new bytes.
    assert b"NEW-PAYLOAD" in result.destination.read_bytes()
    # Sidecar updated to the new checksum (not "old_hash").
    new_sidecar = result.destination.with_suffix(".vtpk.sha256")
    assert new_sidecar.read_text() != "old_hash"
    assert len(new_sidecar.read_text()) == 64
