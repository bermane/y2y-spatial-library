"""End-to-end reconcile tests.

The fixtures synthesize a fully-ingested baseline (one approved dataset
in library/ + a row in inventory.xlsx + a changelog entry) and then
each test perturbs that state and asserts what reconcile reports.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pipeline import ingest, inventory_manager, pending_sheet, reconcile


def _populate_one_dataset(
    project_tree: dict[str, Path],
    valid_gpkg_factory,
    *,
    filename: str = "streams_2024.gpkg",
    category: str = "Water",
    subcategory: str | None = None,
) -> tuple[str, str]:
    """Scan, fill, and approve one dataset. Returns (dataset_id, library-relative path)."""
    valid_gpkg_factory(filename, dest_dir=project_tree["incoming"])
    ingest.scan(project_tree["incoming"], project_tree["processing"], project_tree["rejected"])

    pending_path = project_tree["processing"] / pending_sheet.PENDING_FILENAME
    rows = pending_sheet.load_pending(pending_path)
    row = rows[0]
    row.update(
        ready=True,
        category=category,
        subcategory=subcategory,
        version="1.0",
        source="Test Source",
        license="CC-BY-4.0",
        data_steward="Tester",
        title="Test Title",
        summary="Test summary.",
        description="Test description.",
        tags="test",
        terms_of_use="Test terms.",
        acknowledgements="Test ack.",
    )
    pending_sheet.save_pending(pending_path, [row])

    ingest.approve(
        project_tree["processing"], project_tree["library"],
        project_tree["inventory"], project_tree["changelog"],
        actor="tester",
    )

    rel = f"{category}/{subcategory}/{filename}" if subcategory else f"{category}/{filename}"
    return row["dataset_id"], rel


def _reconcile(project_tree: dict[str, Path], **kwargs: Any) -> reconcile.ReconcileResult:
    return reconcile.reconcile(
        project_tree["library"], project_tree["inventory"],
        project_tree["root"] / "reports", **kwargs,
    )


# --- happy paths --------------------------------------------------------

def test_reconcile_clean_state_has_no_findings(project_tree, valid_gpkg_factory) -> None:
    _populate_one_dataset(project_tree, valid_gpkg_factory)

    result = _reconcile(project_tree)

    assert result.library_files == 1
    assert result.inventory_rows == 1
    assert result.total_findings == 0


def test_reconcile_empty_state_has_no_findings(project_tree) -> None:
    result = _reconcile(project_tree)

    assert result.library_files == 0
    assert result.inventory_rows == 0
    assert result.total_findings == 0


# --- four primary categories -------------------------------------------

def test_reconcile_detects_orphan(project_tree, valid_gpkg_factory) -> None:
    # Drop a file directly into library/ without ingesting
    target_dir = project_tree["library"] / "Water"
    target_dir.mkdir(parents=True, exist_ok=True)
    valid_gpkg_factory("rogue.gpkg", dest_dir=target_dir)

    result = _reconcile(project_tree)

    assert len(result.orphans) == 1
    assert "Water/rogue.gpkg" == result.orphans[0].path


def test_reconcile_detects_ghost(project_tree, valid_gpkg_factory) -> None:
    _, rel_path = _populate_one_dataset(project_tree, valid_gpkg_factory)
    (project_tree["library"] / rel_path).unlink()

    result = _reconcile(project_tree)

    assert len(result.ghosts) == 1
    assert result.ghosts[0].path == rel_path


def test_reconcile_detects_drift_when_file_modified(project_tree, valid_gpkg_factory) -> None:
    _, rel_path = _populate_one_dataset(project_tree, valid_gpkg_factory)
    # Append bytes — changes size + mtime + checksum
    with (project_tree["library"] / rel_path).open("ab") as f:
        f.write(b"extra bytes that bust the snapshot")

    result = _reconcile(project_tree)

    assert len(result.drift) == 1
    # Fast mode catches via size or mtime first
    reason = result.drift[0].reason
    assert "size_bytes" in reason or "mtime" in reason


def test_reconcile_detects_tombstoned_but_present_as_violation(
    project_tree, valid_gpkg_factory,
) -> None:
    _populate_one_dataset(project_tree, valid_gpkg_factory)

    # Mark the row tombstoned but leave the file on disk
    rows = inventory_manager.load_inventory(project_tree["inventory"])
    rows[0]["status"] = "tombstoned"
    inventory_manager.save_inventory(project_tree["inventory"], rows)

    result = _reconcile(project_tree)

    assert len(result.schema_violations) >= 1
    assert any("tombstoned" in v.reason for v in result.schema_violations)
    assert len(result.orphans) == 0  # the row claims it, just disagrees


def test_reconcile_tombstoned_and_absent_is_clean(project_tree, valid_gpkg_factory) -> None:
    _, rel_path = _populate_one_dataset(project_tree, valid_gpkg_factory)
    rows = inventory_manager.load_inventory(project_tree["inventory"])
    rows[0]["status"] = "tombstoned"
    inventory_manager.save_inventory(project_tree["inventory"], rows)
    (project_tree["library"] / rel_path).unlink()

    result = _reconcile(project_tree)

    assert result.total_findings == 0


# --- fast vs deep ------------------------------------------------------

def test_fast_mode_misses_content_swap_with_restored_stat(
    project_tree, valid_gpkg_factory,
) -> None:
    """Same size + same mtime + different content → fast mode cannot catch it."""
    _, rel_path = _populate_one_dataset(project_tree, valid_gpkg_factory)
    file_path = project_tree["library"] / rel_path

    orig = file_path.stat()
    file_path.write_bytes(b"X" * orig.st_size)
    os.utime(file_path, (orig.st_atime, orig.st_mtime))

    fast = _reconcile(project_tree, deep=False)
    # No drift line specifically about checksum; size and mtime match.
    assert all("checksum" not in d.reason for d in fast.drift)


def test_deep_mode_catches_content_swap_with_restored_stat(
    project_tree, valid_gpkg_factory,
) -> None:
    _, rel_path = _populate_one_dataset(project_tree, valid_gpkg_factory)
    file_path = project_tree["library"] / rel_path

    orig = file_path.stat()
    file_path.write_bytes(b"X" * orig.st_size)
    os.utime(file_path, (orig.st_atime, orig.st_mtime))

    deep_result = _reconcile(project_tree, deep=True)
    assert any("checksum" in d.reason for d in deep_result.drift)


# --- rename detection (deep only) --------------------------------------

def test_deep_mode_detects_rename(project_tree, valid_gpkg_factory) -> None:
    _, rel_path = _populate_one_dataset(project_tree, valid_gpkg_factory)
    src = project_tree["library"] / rel_path
    dst = project_tree["library"] / "Water" / "streams_renamed.gpkg"
    src.rename(dst)

    result = _reconcile(project_tree, deep=True)

    assert len(result.renames) == 1
    assert len(result.ghosts) == 0
    assert len(result.orphans) == 0
    assert "streams_2024.gpkg" in result.renames[0].path
    assert "streams_renamed.gpkg" in result.renames[0].path


def test_fast_mode_shows_rename_as_ghost_plus_orphan(
    project_tree, valid_gpkg_factory,
) -> None:
    _, rel_path = _populate_one_dataset(project_tree, valid_gpkg_factory)
    src = project_tree["library"] / rel_path
    dst = project_tree["library"] / "Water" / "streams_renamed.gpkg"
    src.rename(dst)

    result = _reconcile(project_tree, deep=False)

    assert len(result.renames) == 0
    assert len(result.ghosts) == 1
    assert len(result.orphans) == 1


# --- report file -------------------------------------------------------

def test_reconcile_writes_markdown_report(project_tree, valid_gpkg_factory) -> None:
    _populate_one_dataset(project_tree, valid_gpkg_factory)

    result = _reconcile(project_tree, deep=True)

    assert result.report_path.exists()
    text = result.report_path.read_text()
    assert "# Reconcile report" in text
    assert "## Summary" in text
    assert "## Orphans" in text
    assert "## Ghosts" in text
    assert "## Drift" in text
    assert "## Schema violations" in text
    assert "## Renames" in text
    # Filename includes mode and a colon-free timestamp
    assert "_deep" in result.report_path.name
    assert ":" not in result.report_path.name


def test_reconcile_report_filename_includes_mode(project_tree) -> None:
    fast = _reconcile(project_tree, deep=False)
    deep = _reconcile(project_tree, deep=True)
    assert "_fast" in fast.report_path.name
    assert "_deep" in deep.report_path.name
