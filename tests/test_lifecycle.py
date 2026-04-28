"""Tests for post-ingest lifecycle ops: update, rename, tombstone."""

from __future__ import annotations

import pytest

from pipeline import inventory_manager, lifecycle


# --- update -------------------------------------------------------------

def test_update_changes_allowed_field_and_logs(project_tree, populate_dataset) -> None:
    dataset_id, _ = populate_dataset()

    row = lifecycle.update(
        project_tree["inventory"], project_tree["changelog"],
        dataset_id=dataset_id, fields={"summary": "Revised summary."},
        actor="Ethan",
    )

    assert row["summary"] == "Revised summary."
    inv = inventory_manager.load_inventory(project_tree["inventory"])
    assert inv[0]["summary"] == "Revised summary."

    log = project_tree["changelog"].read_text()
    assert "— update — " in log
    assert "summary" in log
    assert "Ethan" in log


def test_update_rejects_locked_field(project_tree, populate_dataset) -> None:
    dataset_id, _ = populate_dataset()

    with pytest.raises(lifecycle.LifecycleError, match="cannot update"):
        lifecycle.update(
            project_tree["inventory"], project_tree["changelog"],
            dataset_id=dataset_id, fields={"checksum_sha256": "0" * 64},
            actor="Ethan",
        )


def test_update_rejects_movement_bound_field(project_tree, populate_dataset) -> None:
    dataset_id, _ = populate_dataset()

    with pytest.raises(lifecycle.LifecycleError, match="cannot update"):
        lifecycle.update(
            project_tree["inventory"], project_tree["changelog"],
            dataset_id=dataset_id, fields={"category": "Climate Resilience"},
            actor="Ethan",
        )


def test_update_rejects_status_tombstoned_via_update(project_tree, populate_dataset) -> None:
    dataset_id, _ = populate_dataset()

    with pytest.raises(lifecycle.LifecycleError, match="status='tombstoned' not allowed"):
        lifecycle.update(
            project_tree["inventory"], project_tree["changelog"],
            dataset_id=dataset_id, fields={"status": "tombstoned"},
            actor="Ethan",
        )


def test_update_unknown_dataset_id_raises(project_tree) -> None:
    with pytest.raises(lifecycle.LifecycleError, match="not found"):
        lifecycle.update(
            project_tree["inventory"], project_tree["changelog"],
            dataset_id="ds_does_not_exi", fields={"summary": "x"}, actor="Ethan",
        )


def test_update_no_change_is_noop(project_tree, populate_dataset) -> None:
    dataset_id, _ = populate_dataset()

    log_before = (
        project_tree["changelog"].read_text() if project_tree["changelog"].exists() else ""
    )
    lifecycle.update(
        project_tree["inventory"], project_tree["changelog"],
        dataset_id=dataset_id, fields={"summary": "Test summary."},  # same as ingest value
        actor="Ethan",
    )
    log_after = project_tree["changelog"].read_text()
    # No new changelog entry should have been written
    assert log_after == log_before


# --- rename -------------------------------------------------------------

def test_rename_moves_file_and_updates_inventory(project_tree, populate_dataset) -> None:
    dataset_id, old_rel = populate_dataset()  # Water/streams_2024.gpkg

    new_path = "Water/streams_v2.gpkg"
    row = lifecycle.rename(
        project_tree["inventory"], project_tree["changelog"], project_tree["library"],
        dataset_id=dataset_id, new_path=new_path, actor="Ethan",
    )

    assert (project_tree["library"] / new_path).exists()
    assert not (project_tree["library"] / old_rel).exists()
    assert row["file_path"] == new_path
    assert row["category"] == "Water"
    assert row["subcategory"] is None

    log = project_tree["changelog"].read_text()
    assert "— rename — " in log
    assert "streams_2024.gpkg" in log and "streams_v2.gpkg" in log


def test_rename_can_change_category(project_tree, populate_dataset) -> None:
    dataset_id, old_rel = populate_dataset()

    new_path = "Connectivity_Wildlife_Movement/streams_2024.gpkg"
    row = lifecycle.rename(
        project_tree["inventory"], project_tree["changelog"], project_tree["library"],
        dataset_id=dataset_id, new_path=new_path, actor="Ethan",
    )

    assert (project_tree["library"] / new_path).exists()
    # rename writes the *display* category name back to the inventory.
    assert row["category"] == "Connectivity & Wildlife Movement"


def test_rename_records_already_moved_file(project_tree, populate_dataset) -> None:
    """Steward moved the file manually; rename should record the new path without touching disk."""
    dataset_id, old_rel = populate_dataset()
    old_full = project_tree["library"] / old_rel
    new_rel = "Water/streams_renamed.gpkg"
    new_full = project_tree["library"] / new_rel
    new_full.parent.mkdir(parents=True, exist_ok=True)
    old_full.rename(new_full)  # manual move

    row = lifecycle.rename(
        project_tree["inventory"], project_tree["changelog"], project_tree["library"],
        dataset_id=dataset_id, new_path=new_rel, actor="Ethan",
    )

    assert row["file_path"] == new_rel
    assert new_full.exists()
    log = project_tree["changelog"].read_text()
    assert "file already at new path" in log


def test_rename_rejects_invalid_filename(project_tree, populate_dataset) -> None:
    dataset_id, _ = populate_dataset()
    with pytest.raises(lifecycle.LifecycleError, match="naming convention"):
        lifecycle.rename(
            project_tree["inventory"], project_tree["changelog"], project_tree["library"],
            dataset_id=dataset_id, new_path="Water/Streams.gpkg",  # uppercase
            actor="Ethan",
        )


def test_rename_rejects_unknown_category(project_tree, populate_dataset) -> None:
    dataset_id, _ = populate_dataset()
    with pytest.raises(lifecycle.LifecycleError, match="not one of the"):
        lifecycle.rename(
            project_tree["inventory"], project_tree["changelog"], project_tree["library"],
            dataset_id=dataset_id, new_path="Made_Up/streams.gpkg", actor="Ethan",
        )


def test_rename_rejects_subcategory_for_non_species(project_tree, populate_dataset) -> None:
    dataset_id, _ = populate_dataset()
    with pytest.raises(lifecycle.LifecycleError, match="admits no subcategory"):
        lifecycle.rename(
            project_tree["inventory"], project_tree["changelog"], project_tree["library"],
            dataset_id=dataset_id, new_path="Water/Streams/streams.gpkg", actor="Ethan",
        )


def test_rename_into_species_requires_valid_subcategory(project_tree, populate_dataset) -> None:
    dataset_id, _ = populate_dataset()
    with pytest.raises(lifecycle.LifecycleError, match="not valid for category"):
        lifecycle.rename(
            project_tree["inventory"], project_tree["changelog"], project_tree["library"],
            dataset_id=dataset_id, new_path="Species/Cougar/streams.gpkg", actor="Ethan",
        )


def test_rename_rejects_target_collision(project_tree, populate_dataset, valid_gpkg_factory) -> None:
    dataset_id, _ = populate_dataset()
    # Place an existing file at the target
    (project_tree["library"] / "Water" / "streams_v2.gpkg").parent.mkdir(parents=True, exist_ok=True)
    valid_gpkg_factory("streams_v2.gpkg", dest_dir=project_tree["library"] / "Water")

    with pytest.raises(lifecycle.LifecycleError, match="both old"):
        lifecycle.rename(
            project_tree["inventory"], project_tree["changelog"], project_tree["library"],
            dataset_id=dataset_id, new_path="Water/streams_v2.gpkg", actor="Ethan",
        )


# --- tombstone ----------------------------------------------------------

def test_tombstone_marks_row_and_deletes_file(project_tree, populate_dataset) -> None:
    dataset_id, rel = populate_dataset()
    full = project_tree["library"] / rel
    assert full.exists()

    lifecycle.tombstone(
        project_tree["inventory"], project_tree["changelog"], project_tree["library"],
        dataset_id=dataset_id, actor="Ethan", reason="superseded by v2",
    )

    assert not full.exists()
    inv = inventory_manager.load_inventory(project_tree["inventory"])
    assert inv[0]["status"] == "tombstoned"

    log = project_tree["changelog"].read_text()
    assert "— remove — " in log
    assert "superseded by v2" in log


def test_tombstone_handles_already_absent_file(project_tree, populate_dataset) -> None:
    dataset_id, rel = populate_dataset()
    (project_tree["library"] / rel).unlink()  # manual deletion

    lifecycle.tombstone(
        project_tree["inventory"], project_tree["changelog"], project_tree["library"],
        dataset_id=dataset_id, actor="Ethan",
    )

    inv = inventory_manager.load_inventory(project_tree["inventory"])
    assert inv[0]["status"] == "tombstoned"
    log = project_tree["changelog"].read_text()
    assert "file already absent" in log


# --- refresh ------------------------------------------------------------

def test_refresh_applies_when_mtime_changed(project_tree, populate_dataset) -> None:
    """mtime change without content change still gets recorded."""
    import os

    dataset_id, rel = populate_dataset()
    full = project_tree["library"] / rel

    inv_before = inventory_manager.load_inventory(project_tree["inventory"])[0]

    st = full.stat()
    os.utime(full, (st.st_atime, st.st_mtime + 60))

    row = lifecycle.refresh(
        project_tree["inventory"], project_tree["changelog"], project_tree["library"],
        dataset_id=dataset_id, actor="ethan",
    )

    assert row["mtime"] != inv_before["mtime"]
    assert row["checksum_sha256"] == inv_before["checksum_sha256"]
    log = project_tree["changelog"].read_text()
    assert "— refresh — " in log
    assert "mtime" in log


def test_refresh_records_size_and_checksum_diff(project_tree, populate_dataset) -> None:
    dataset_id, rel = populate_dataset()
    full = project_tree["library"] / rel

    inv_before = inventory_manager.load_inventory(project_tree["inventory"])[0]
    # Append bytes — changes size + mtime + checksum, file still canonical (GPKG/SQLite tolerates trailing junk)
    with full.open("ab") as f:
        f.write(b"trailing bytes that don't break canonical validity")

    row = lifecycle.refresh(
        project_tree["inventory"], project_tree["changelog"], project_tree["library"],
        dataset_id=dataset_id, actor="ethan",
    )

    assert row["checksum_sha256"] != inv_before["checksum_sha256"]
    assert int(row["size_bytes"]) > int(inv_before["size_bytes"])
    log = project_tree["changelog"].read_text()
    assert "checksum_sha256" in log


def test_refresh_noop_when_no_drift(project_tree, populate_dataset) -> None:
    dataset_id, _ = populate_dataset()
    log_before = project_tree["changelog"].read_text()

    lifecycle.refresh(
        project_tree["inventory"], project_tree["changelog"], project_tree["library"],
        dataset_id=dataset_id, actor="ethan",
    )

    log_after = project_tree["changelog"].read_text()
    assert log_after == log_before  # no entry appended


def test_refresh_rejects_missing_file(project_tree, populate_dataset) -> None:
    dataset_id, rel = populate_dataset()
    (project_tree["library"] / rel).unlink()

    with pytest.raises(lifecycle.LifecycleError, match="not found"):
        lifecycle.refresh(
            project_tree["inventory"], project_tree["changelog"], project_tree["library"],
            dataset_id=dataset_id, actor="ethan",
        )


def test_refresh_rejects_tombstoned(project_tree, populate_dataset) -> None:
    dataset_id, _ = populate_dataset()
    lifecycle.tombstone(
        project_tree["inventory"], project_tree["changelog"], project_tree["library"],
        dataset_id=dataset_id, actor="ethan",
    )

    with pytest.raises(lifecycle.LifecycleError, match="tombstoned"):
        lifecycle.refresh(
            project_tree["inventory"], project_tree["changelog"], project_tree["library"],
            dataset_id=dataset_id, actor="ethan",
        )


def test_refresh_rejects_unknown_dataset_id(project_tree) -> None:
    with pytest.raises(lifecycle.LifecycleError, match="not found"):
        lifecycle.refresh(
            project_tree["inventory"], project_tree["changelog"], project_tree["library"],
            dataset_id="ds_unknownnnnn", actor="ethan",
        )


def test_refresh_rejects_when_canonical_validation_fails(project_tree, populate_dataset) -> None:
    """If the in-place edit broke the file, refresh refuses to record bad state."""
    dataset_id, rel = populate_dataset()
    full = project_tree["library"] / rel
    full.write_bytes(b"not a gpkg anymore")

    with pytest.raises(lifecycle.LifecycleError, match="canonical validators"):
        lifecycle.refresh(
            project_tree["inventory"], project_tree["changelog"], project_tree["library"],
            dataset_id=dataset_id, actor="ethan",
        )


def test_tombstone_rejects_already_tombstoned(project_tree, populate_dataset) -> None:
    dataset_id, _ = populate_dataset()
    lifecycle.tombstone(
        project_tree["inventory"], project_tree["changelog"], project_tree["library"],
        dataset_id=dataset_id, actor="Ethan",
    )
    with pytest.raises(lifecycle.LifecycleError, match="already tombstoned"):
        lifecycle.tombstone(
            project_tree["inventory"], project_tree["changelog"], project_tree["library"],
            dataset_id=dataset_id, actor="Ethan",
        )
