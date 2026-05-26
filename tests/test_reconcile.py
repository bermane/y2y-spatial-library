"""End-to-end reconcile tests.

Each test populates a fully-ingested baseline (one approved dataset
in ``library/spatial/`` + a row in ``inventory.db`` + a changelog row)
and then perturbs that state and asserts what reconcile reports.

Post-migration to SQLite (DESIGN.md §12): reconcile takes ``db_path``
instead of separate ``inventory_path`` / ``changelog_path``. The
xlsx-lock guard is gone; the catalogue is SQLite and not subject to
Excel locks.
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
        project_tree["db"],
        actor="tester",
    )

    rel = f"{category}/{subcategory}/{filename}" if subcategory else f"{category}/{filename}"
    return row["dataset_id"], rel


def _reconcile(project_tree: dict[str, Path], **kwargs: Any) -> reconcile.ReconcileResult:
    kwargs.setdefault("actor", "tester")
    return reconcile.reconcile(
        project_tree["library"], project_tree["db"],
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


def test_reconcile_auto_resolves_drift_when_file_still_canonical(
    project_tree, valid_gpkg_factory,
) -> None:
    """Canonical-passing drift is auto-resolved via lifecycle.refresh."""
    dataset_id, rel_path = _populate_one_dataset(project_tree, valid_gpkg_factory)
    # Append bytes — changes size + mtime + checksum. SQLite/GPKG tolerates
    # trailing junk so the file stays readable and canonical.
    with (project_tree["library"] / rel_path).open("ab") as f:
        f.write(b"extra bytes that change the snapshot but stay canonical")

    result = _reconcile(project_tree)

    # No outstanding drift — auto-resolved via refresh.
    assert len(result.drift) == 0
    assert len(result.auto_resolved) == 1
    assert "size_bytes" in result.auto_resolved[0].reason

    # Inventory snapshot updated to match the file
    row = inventory_manager.get_dataset(project_tree["db"], dataset_id)
    assert int(row["size_bytes"]) == (project_tree["library"] / rel_path).stat().st_size

    # Changelog records the auto-refresh
    log = inventory_manager.load_changelog(project_tree["db"])
    assert any(
        r["action"] == "refresh" and r["dataset_id"] == dataset_id for r in log
    )


def test_reconcile_does_not_auto_resolve_when_file_no_longer_canonical(
    project_tree, valid_gpkg_factory,
) -> None:
    """If the file fails canonical validators, drift stays as an action item."""
    _, rel_path = _populate_one_dataset(project_tree, valid_gpkg_factory)
    # Overwrite with garbage — file is no longer a valid GPKG.
    (project_tree["library"] / rel_path).write_bytes(b"this is not a GPKG anymore")

    result = _reconcile(project_tree)

    # Schema violation fired; drift kept as action item; no auto-resolve.
    assert len(result.schema_violations) >= 1
    assert len(result.drift) >= 1
    assert len(result.auto_resolved) == 0


def test_reconcile_apply_drift_can_be_disabled(project_tree, valid_gpkg_factory) -> None:
    """apply_drift=False reverts to read-only behaviour for drift findings."""
    _, rel_path = _populate_one_dataset(project_tree, valid_gpkg_factory)
    with (project_tree["library"] / rel_path).open("ab") as f:
        f.write(b"more bytes")

    result = _reconcile(project_tree, apply_drift=False)

    assert len(result.drift) == 1
    assert len(result.auto_resolved) == 0


def test_reconcile_detects_tombstoned_but_present_as_violation(
    project_tree, valid_gpkg_factory,
) -> None:
    dataset_id, _ = _populate_one_dataset(project_tree, valid_gpkg_factory)

    # Mark the row tombstoned but leave the file on disk
    inventory_manager.update_dataset(
        project_tree["db"], dataset_id, {"status": "tombstoned"}
    )

    result = _reconcile(project_tree)

    assert len(result.schema_violations) >= 1
    assert any("tombstoned" in v.reason for v in result.schema_violations)
    assert len(result.orphans) == 0  # the row claims it, just disagrees


def test_reconcile_tombstoned_and_absent_is_clean(project_tree, valid_gpkg_factory) -> None:
    dataset_id, rel_path = _populate_one_dataset(project_tree, valid_gpkg_factory)
    inventory_manager.update_dataset(
        project_tree["db"], dataset_id, {"status": "tombstoned"}
    )
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


# --- VTPK invariants (rev 3) -------------------------------------------

def test_reconcile_flags_missing_vtpk_for_vtl_row(
    project_tree, valid_gpkg_factory,
) -> None:
    """An active row whose ``agol_target='vector-tile-layer'`` but
    whose canonical ``library/vtpk/<stem>.vtpk`` is absent is
    reported as a missing-VTPK issue. Reconcile exits non-zero (via
    total_findings > 0) so the steward sees it."""
    dataset_id, _ = _populate_one_dataset(project_tree, valid_gpkg_factory)

    # Switch the row's agol_target to vector-tile-layer; no VTPK
    # exists on disk for it yet.
    inventory_manager.update_dataset(
        project_tree["db"], dataset_id,
        {"agol_target": "vector-tile-layer"},
    )

    result = _reconcile(project_tree)
    assert len(result.vtpk_missing) == 1
    finding = result.vtpk_missing[0]
    assert finding.dataset_id == dataset_id
    assert "missing" in finding.reason
    assert "Build VTPK in ArcGIS Pro" in finding.reason


def test_reconcile_flags_stale_vtpk_when_gpkg_newer(
    project_tree, valid_gpkg_factory,
) -> None:
    """An ingested VTPK whose mtime is older than the source GPKG
    is reported as stale. Stewards see this when they've updated
    the GPKG via lifecycle.refresh but forgotten to rebuild the
    VTPK in Pro."""
    dataset_id, rel = _populate_one_dataset(project_tree, valid_gpkg_factory)
    inventory_manager.update_dataset(
        project_tree["db"], dataset_id,
        {"agol_target": "vector-tile-layer"},
    )

    # Plant a VTPK at the canonical location.
    vtpk_dir = project_tree["library"].parent / "vtpk"
    vtpk_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(rel).stem
    vtpk_path = vtpk_dir / f"{stem}.vtpk"
    vtpk_path.write_bytes(b"PK\x03\x04fake-vtpk")

    # Make the GPKG mtime newer than the VTPK by 60 seconds.
    gpkg_path = project_tree["library"] / rel
    vtpk_mtime = vtpk_path.stat().st_mtime
    new_mtime = vtpk_mtime + 60
    os.utime(gpkg_path, (new_mtime, new_mtime))

    result = _reconcile(project_tree)
    assert len(result.vtpk_missing) == 0
    assert len(result.vtpk_stale) == 1
    finding = result.vtpk_stale[0]
    assert finding.dataset_id == dataset_id
    assert "stale" not in finding.reason.lower() or "modified after" in finding.reason


def test_reconcile_does_not_flag_vtpk_for_non_vtl_rows(
    project_tree, valid_gpkg_factory,
) -> None:
    """Rows whose ``agol_target`` is not 'vector-tile-layer' are
    not subject to the VTPK invariants — no false positives for
    feature-layer / imagery-layer rows."""
    dataset_id, _ = _populate_one_dataset(project_tree, valid_gpkg_factory)
    # Default agol_target for a vector is 'feature-layer'. Verify
    # reconcile doesn't fire VTPK findings.
    result = _reconcile(project_tree)
    assert len(result.vtpk_missing) == 0
    assert len(result.vtpk_stale) == 0
