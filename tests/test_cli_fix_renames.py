"""CLI tests for ``y2y reconcile --fix-renames``.

Uses click's CliRunner to drive the full command including the
interactive confirmation prompts.
"""

from __future__ import annotations

from click.testing import CliRunner

from pipeline import inventory_manager
from pipeline.__main__ import cli


def _move_file_manually(project_tree, old_rel: str, new_rel: str) -> None:
    """Create a ghost+orphan situation by moving a file outside the pipeline."""
    old_full = project_tree["library"] / old_rel
    new_full = project_tree["library"] / new_rel
    new_full.parent.mkdir(parents=True, exist_ok=True)
    old_full.rename(new_full)


def test_fix_renames_applies_when_confirmed(project_tree, populate_dataset) -> None:
    dataset_id, old_rel = populate_dataset()
    new_rel = "Water/streams_renamed.gpkg"
    _move_file_manually(project_tree, old_rel, new_rel)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--root", str(project_tree["root"]), "reconcile", "--fix-renames"],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert "fix-renames done" in result.output
    assert "applied: " in result.output

    inv = inventory_manager.load_inventory(project_tree["inventory"])
    assert inv[0]["file_path"] == new_rel
    assert inv[0]["dataset_id"] == dataset_id

    log = project_tree["changelog"].read_text()
    assert "— rename — " in log
    assert dataset_id in log


def test_fix_renames_skips_when_declined(project_tree, populate_dataset) -> None:
    _, old_rel = populate_dataset()
    new_rel = "Water/streams_renamed.gpkg"
    _move_file_manually(project_tree, old_rel, new_rel)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--root", str(project_tree["root"]), "reconcile", "--fix-renames"],
        input="n\n",
    )

    assert result.exit_code == 0, result.output
    assert "skipped: 1" in result.output

    # Inventory file_path unchanged (still points at the old location)
    inv = inventory_manager.load_inventory(project_tree["inventory"])
    assert inv[0]["file_path"] == old_rel


def test_fix_renames_with_no_candidates(project_tree, populate_dataset) -> None:
    populate_dataset()  # clean state
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--root", str(project_tree["root"]), "reconcile", "--fix-renames"],
    )

    assert result.exit_code == 0, result.output
    assert "no rename candidates" in result.output


def test_fix_renames_implies_deep(project_tree, populate_dataset) -> None:
    """--fix-renames runs reconcile in deep mode even if --deep wasn't passed."""
    _, old_rel = populate_dataset()
    new_rel = "Water/streams_v2.gpkg"
    _move_file_manually(project_tree, old_rel, new_rel)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--root", str(project_tree["root"]), "reconcile", "--fix-renames"],
        input="n\n",
    )

    # Deep-mode summary should be printed (and only deep mode detects renames)
    assert "(mode: deep)" in result.output
