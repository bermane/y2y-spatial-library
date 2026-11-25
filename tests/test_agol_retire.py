"""Deprecated and tombstoned datasets leave ArcGIS Online.

A row is "due for removal" when its status is ``deprecated`` or
``tombstoned`` and it still carries an ``agol_item_id``. The auto-sync
hook removes it right away; ``agol-sync reconcile`` is the safety net.
The library file (for deprecated rows) and the catalogue row are kept.

All AGOL access is mocked. Nothing here contacts ArcGIS Online.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from pipeline import agol_config, agol_sync, inventory_manager, lifecycle
from pipeline.__main__ import cli
from tests.test_agol_push import _full_row


@pytest.fixture
def config() -> agol_config.AgolConfig:
    return agol_config.AgolConfig(
        conservation_atlas_group_id="cached_group_xyz",
    )


def _published_row(db: Path, dataset_id: str = "ds_old", **overrides) -> dict:
    row = _full_row(
        dataset_id=dataset_id, file_path=f"Water/{dataset_id}.gpkg",
        sync_status="clean", agol_item_id=f"svc_{dataset_id}",
        agol_format="feature-layer",
    )
    row["agol_published_at"] = "2026-01-01T00:00:00Z"
    row["last_synced_at"] = "2026-01-02T00:00:00Z"
    row.update(overrides)
    inventory_manager.insert_dataset(db, row)
    return row


def _fake_gis(*, delete_error: Exception | None = None, delete_returns=True):
    """A GIS whose content.get() returns one service + one source item."""
    source_item = MagicMock()
    service_item = MagicMock()
    service_item.id = "svc"
    service_item.related_items.return_value = [source_item]
    if delete_error is not None:
        service_item.delete.side_effect = delete_error
    else:
        service_item.delete.return_value = delete_returns
    gis = MagicMock()
    gis.content.get.return_value = service_item
    return gis, service_item, source_item


def _auto_push_on(monkeypatch, gis) -> MagicMock:
    monkeypatch.setenv("Y2Y_AGOL_AUTO_PUSH", "true")
    get_gis = MagicMock(return_value=gis)
    monkeypatch.setattr(agol_sync, "get_gis", get_gis)
    return get_gis


# --- deprecating through `update` -------------------------------------


def test_deprecating_published_row_removes_agol_items(
    project_tree, monkeypatch,
) -> None:
    db = project_tree["db"]
    _published_row(db)
    gis, service_item, source_item = _fake_gis()
    _auto_push_on(monkeypatch, gis)

    lifecycle.update(
        db, dataset_id="ds_old", fields={"status": "deprecated"},
        actor="tester", library_root=project_tree["library"], auto_push=True,
    )

    source_item.delete.assert_called_once_with(permanent=True)
    service_item.delete.assert_called_once_with(permanent=True)
    fresh = inventory_manager.get_dataset(db, "ds_old")
    assert fresh["status"] == "deprecated"
    assert fresh["agol_item_id"] is None
    assert fresh["agol_published_at"] is None
    assert fresh["sync_status"] == "unpublished"
    # The old item id survives in the changelog for the audit trail.
    log = inventory_manager.load_changelog(db)
    assert any("svc_ds_old" in (r["note"] or "") for r in log)


def test_deprecating_keeps_library_file_and_row(
    project_tree, populate_dataset, monkeypatch,
) -> None:
    db = project_tree["db"]
    dataset_id, rel = populate_dataset()
    inventory_manager.update_dataset(db, dataset_id, {
        "agol_item_id": "svc_real", "sync_status": "clean",
        "agol_format": "feature-layer",
    })
    gis, _, _ = _fake_gis()
    _auto_push_on(monkeypatch, gis)

    lifecycle.update(
        db, dataset_id=dataset_id, fields={"status": "deprecated"},
        actor="tester", library_root=project_tree["library"], auto_push=True,
    )

    assert (project_tree["library"] / rel).exists()
    fresh = inventory_manager.get_dataset(db, dataset_id)
    assert fresh is not None
    assert fresh["status"] == "deprecated"
    assert fresh["agol_item_id"] is None


def test_deprecating_with_auto_push_off_does_not_contact_agol(
    project_tree, monkeypatch,
) -> None:
    db = project_tree["db"]
    _published_row(db)
    monkeypatch.setenv("Y2Y_AGOL_AUTO_PUSH", "false")
    get_gis = MagicMock()
    monkeypatch.setattr(agol_sync, "get_gis", get_gis)

    lifecycle.update(
        db, dataset_id="ds_old", fields={"status": "deprecated"},
        actor="tester", library_root=project_tree["library"], auto_push=True,
    )

    get_gis.assert_not_called()
    fresh = inventory_manager.get_dataset(db, "ds_old")
    assert fresh["status"] == "deprecated"
    assert fresh["agol_item_id"] == "svc_ds_old"  # still due for removal
    # Not stranded in pending_push: deprecated rows are never marked dirty.
    assert fresh["sync_status"] == "clean"


def test_deprecating_when_agol_unreachable_keeps_link_and_audits(
    project_tree, monkeypatch,
) -> None:
    db = project_tree["db"]
    _published_row(db)
    monkeypatch.setenv("Y2Y_AGOL_AUTO_PUSH", "true")
    monkeypatch.setattr(
        agol_sync, "get_gis",
        MagicMock(side_effect=agol_sync.AgolAuthError("offline")),
    )

    lifecycle.update(
        db, dataset_id="ds_old", fields={"status": "deprecated"},
        actor="tester", library_root=project_tree["library"], auto_push=True,
    )

    fresh = inventory_manager.get_dataset(db, "ds_old")
    assert fresh["status"] == "deprecated"  # the catalogue write landed
    assert fresh["agol_item_id"] == "svc_ds_old"
    log = inventory_manager.load_changelog(db)
    deferred = [r for r in log if "AGOL removal deferred" in (r["note"] or "")]
    assert len(deferred) == 1


def test_deprecating_unpublished_row_does_nothing_on_agol(
    project_tree, monkeypatch,
) -> None:
    db = project_tree["db"]
    row = _full_row(dataset_id="ds_never", file_path="Water/n.gpkg")
    inventory_manager.insert_dataset(db, row)
    gis, _, _ = _fake_gis()
    get_gis = _auto_push_on(monkeypatch, gis)

    lifecycle.update(
        db, dataset_id="ds_never", fields={"status": "deprecated"},
        actor="tester", library_root=project_tree["library"], auto_push=True,
    )

    get_gis.assert_not_called()
    fresh = inventory_manager.get_dataset(db, "ds_never")
    assert fresh["status"] == "deprecated"
    assert fresh["sync_status"] == "unpublished"


def test_reactivating_publishes_again_as_new_item(
    project_tree, monkeypatch,
) -> None:
    """deprecated + unpublished -> active hands the row to push()."""
    db = project_tree["db"]
    row = _full_row(dataset_id="ds_back", file_path="Water/b.gpkg")
    row["status"] = "deprecated"
    inventory_manager.insert_dataset(db, row)
    _auto_push_on(monkeypatch, MagicMock())

    def _fake_push(db_path, dataset_id, gis, config, **kwargs):
        inventory_manager.update_dataset(db_path, dataset_id, {
            "agol_item_id": "svc_new", "sync_status": "clean",
        })
        return agol_sync.SyncResult(
            dataset_id=dataset_id, action="push",
            sync_status_before="unpublished", sync_status_after="clean",
            agol_item_id="svc_new", note="ok",
        )

    push_mock = MagicMock(side_effect=_fake_push)
    monkeypatch.setattr(agol_sync, "push", push_mock)

    lifecycle.update(
        db, dataset_id="ds_back", fields={"status": "active"},
        actor="tester", library_root=project_tree["library"], auto_push=True,
    )

    push_mock.assert_called_once()
    fresh = inventory_manager.get_dataset(db, "ds_back")
    assert fresh["status"] == "active"
    assert fresh["agol_item_id"] == "svc_new"


# --- failed deletions keep the link ------------------------------------


@pytest.mark.parametrize("failure", ["raises", "returns_false"])
def test_unpublish_keeps_link_when_service_deletion_fails(
    project_tree, config, failure,
) -> None:
    db = project_tree["db"]
    _published_row(db)
    if failure == "raises":
        gis, _, _ = _fake_gis(delete_error=Exception("protected"))
    else:
        gis, _, _ = _fake_gis(delete_returns=False)

    result = agol_sync.unpublish(db, "ds_old", gis, config, actor="tester")

    assert result.error
    assert result.sync_status_after == "error"
    assert result.agol_item_id == "svc_ds_old"
    fresh = inventory_manager.get_dataset(db, "ds_old")
    assert fresh["agol_item_id"] == "svc_ds_old"
    assert fresh["agol_published_at"] == "2026-01-01T00:00:00Z"
    assert fresh["sync_status"] == "error"
    assert "unpublish failed" in (fresh["internal_notes"] or "")
    log = inventory_manager.load_changelog(db)
    assert any("unpublish failed" in (r["note"] or "") for r in log)


def test_unpublish_turns_off_delete_protection_first(
    project_tree, config,
) -> None:
    """An Authoritative layer is delete-protected on AGOL; retire clears it."""
    db = project_tree["db"]
    _published_row(db)
    gis, service_item, source_item = _fake_gis()
    service_item.protected = True
    service_item.protect.return_value = {"success": True}
    calls: list[str] = []
    service_item.protect.side_effect = (
        lambda **kw: calls.append("unprotect") or {"success": True}
    )
    source_item.delete.side_effect = lambda **kw: calls.append("source") or True
    service_item.delete.side_effect = lambda **kw: calls.append("service") or True

    result = agol_sync.unpublish(db, "ds_old", gis, config, actor="tester")

    assert result.error is None
    service_item.protect.assert_called_once_with(enable=False)
    assert calls == ["unprotect", "source", "service"]
    assert inventory_manager.get_dataset(db, "ds_old")["agol_item_id"] is None


def test_unpublish_leaves_unprotected_items_alone(project_tree, config) -> None:
    db = project_tree["db"]
    _published_row(db)
    gis, service_item, _ = _fake_gis()
    service_item.protected = False

    agol_sync.unpublish(db, "ds_old", gis, config, actor="tester")

    service_item.protect.assert_not_called()
    service_item.delete.assert_called_once()


@pytest.mark.parametrize("failure", ["raises", "success_false"])
def test_unpublish_deletes_nothing_when_protection_stays_on(
    project_tree, config, failure,
) -> None:
    db = project_tree["db"]
    _published_row(db)
    gis, service_item, source_item = _fake_gis()
    service_item.protected = True
    if failure == "raises":
        service_item.protect.side_effect = Exception("no privilege")
    else:
        service_item.protect.return_value = {"success": False}

    result = agol_sync.unpublish(db, "ds_old", gis, config, actor="tester")

    assert result.error
    source_item.delete.assert_not_called()
    service_item.delete.assert_not_called()
    fresh = inventory_manager.get_dataset(db, "ds_old")
    assert fresh["agol_item_id"] == "svc_ds_old"
    assert fresh["sync_status"] == "error"
    assert "delete protection" in (fresh["internal_notes"] or "")


def test_unpublish_source_only_failure_still_clears_link(
    project_tree, config,
) -> None:
    db = project_tree["db"]
    _published_row(db)
    gis, _, source_item = _fake_gis()
    source_item.delete.side_effect = Exception("source stuck")

    result = agol_sync.unpublish(db, "ds_old", gis, config, actor="tester")

    assert result.error is None
    fresh = inventory_manager.get_dataset(db, "ds_old")
    assert fresh["agol_item_id"] is None
    assert "unpublish warnings" in (fresh["internal_notes"] or "")


# --- reconcile is the safety net ---------------------------------------


def _reconcile(project_tree, gis, config, **kwargs):
    return agol_sync.reconcile_bidirectional(
        project_tree["db"], gis, config,
        library_root=project_tree["library"], actor="tester",
        reports_dir=project_tree["root"] / "reports", **kwargs,
    )


@pytest.mark.parametrize("status", ["deprecated", "tombstoned"])
def test_reconcile_removes_retired_rows_still_linked(
    project_tree, config, status,
) -> None:
    db = project_tree["db"]
    _published_row(db, status=status)
    gis, service_item, _ = _fake_gis()

    report = _reconcile(project_tree, gis, config)

    service_item.delete.assert_called_once_with(permanent=True)
    assert report.counts_by_bucket == {"removed_retired": 1}
    fresh = inventory_manager.get_dataset(db, "ds_old")
    assert fresh["status"] == status
    assert fresh["agol_item_id"] is None
    assert "Removed from AGOL" in report.report_path.read_text(encoding="utf-8")


def test_reconcile_dry_run_reports_removal_without_deleting(
    project_tree, config,
) -> None:
    db = project_tree["db"]
    _published_row(db, status="deprecated")
    gis, service_item, _ = _fake_gis()

    report = _reconcile(project_tree, gis, config, dry_run=True)

    service_item.delete.assert_not_called()
    gis.content.get.assert_not_called()
    assert "would remove AGOL item" in report.outcomes[0].note
    fresh = inventory_manager.get_dataset(db, "ds_old")
    assert fresh["agol_item_id"] == "svc_ds_old"
    assert fresh["sync_status"] == "clean"


def test_reconcile_reports_failed_removal_and_retries_next_run(
    project_tree, config,
) -> None:
    db = project_tree["db"]
    _published_row(db, status="deprecated")
    gis, _, _ = _fake_gis(delete_error=Exception("protected"))

    report = _reconcile(project_tree, gis, config)
    assert report.counts_by_bucket == {"remove_failed": 1}
    assert inventory_manager.get_dataset(db, "ds_old")["agol_item_id"] == "svc_ds_old"

    gis_ok, _, _ = _fake_gis()
    report = _reconcile(project_tree, gis_ok, config)
    assert report.counts_by_bucket == {"removed_retired": 1}
    assert inventory_manager.get_dataset(db, "ds_old")["agol_item_id"] is None


def test_reconcile_leaves_unlinked_retired_rows_alone(
    project_tree, config,
) -> None:
    db = project_tree["db"]
    row = _full_row(dataset_id="ds_never", file_path="Water/n.gpkg")
    row["status"] = "deprecated"
    inventory_manager.insert_dataset(db, row)
    gis, _, _ = _fake_gis()

    report = _reconcile(project_tree, gis, config)

    assert report.outcomes == []
    gis.content.get.assert_not_called()


# --- tombstone ---------------------------------------------------------


def test_tombstone_removes_agol_items(project_tree, monkeypatch) -> None:
    db = project_tree["db"]
    _published_row(db)
    gis, service_item, _ = _fake_gis()
    _auto_push_on(monkeypatch, gis)

    lifecycle.tombstone(
        db, project_tree["library"],
        dataset_id="ds_old", actor="tester", auto_push=True,
    )

    service_item.delete.assert_called_once_with(permanent=True)
    fresh = inventory_manager.get_dataset(db, "ds_old")
    assert fresh["status"] == "tombstoned"
    assert fresh["agol_item_id"] is None


# --- CLI ---------------------------------------------------------------


def _invoke(project_tree, args, **kwargs):
    return CliRunner().invoke(
        cli, ["--root", str(project_tree["root"]), *args], **kwargs,
    )


def test_cli_update_declined_changes_nothing(project_tree, monkeypatch) -> None:
    db = project_tree["db"]
    _published_row(db)
    gis, service_item, _ = _fake_gis()
    _auto_push_on(monkeypatch, gis)

    result = _invoke(
        project_tree, ["update", "ds_old", "--set", "status=deprecated"],
        input="n\n",
    )

    assert result.exit_code != 0
    assert "permanently deletes" in result.output
    service_item.delete.assert_not_called()
    fresh = inventory_manager.get_dataset(db, "ds_old")
    assert fresh["status"] == "active"
    assert fresh["agol_item_id"] == "svc_ds_old"


def test_cli_update_confirmed_removes_layer(project_tree, monkeypatch) -> None:
    db = project_tree["db"]
    _published_row(db)
    gis, service_item, _ = _fake_gis()
    _auto_push_on(monkeypatch, gis)

    result = _invoke(
        project_tree, ["update", "ds_old", "--set", "status=deprecated"],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert "removed from ArcGIS Online" in result.output
    service_item.delete.assert_called_once_with(permanent=True)
    assert inventory_manager.get_dataset(db, "ds_old")["agol_item_id"] is None


def test_cli_update_yes_skips_prompt(project_tree, monkeypatch) -> None:
    db = project_tree["db"]
    _published_row(db)
    gis, _, _ = _fake_gis()
    _auto_push_on(monkeypatch, gis)

    result = _invoke(
        project_tree,
        ["update", "ds_old", "--set", "status=deprecated", "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert inventory_manager.get_dataset(db, "ds_old")["status"] == "deprecated"


def test_cli_update_auto_push_off_notes_later_removal(
    project_tree, monkeypatch,
) -> None:
    db = project_tree["db"]
    _published_row(db)
    monkeypatch.setenv("Y2Y_AGOL_AUTO_PUSH", "false")
    get_gis = MagicMock()
    monkeypatch.setattr(agol_sync, "get_gis", get_gis)

    result = _invoke(
        project_tree, ["update", "ds_old", "--set", "status=deprecated"],
    )

    assert result.exit_code == 0, result.output
    assert "Auto-push is off" in result.output
    get_gis.assert_not_called()
    fresh = inventory_manager.get_dataset(db, "ds_old")
    assert fresh["status"] == "deprecated"
    assert fresh["agol_item_id"] == "svc_ds_old"


def test_cli_update_other_fields_do_not_prompt(project_tree, monkeypatch) -> None:
    db = project_tree["db"]
    _published_row(db)
    monkeypatch.setenv("Y2Y_AGOL_AUTO_PUSH", "false")

    result = _invoke(project_tree, ["update", "ds_old", "--set", "title=New"])

    assert result.exit_code == 0, result.output
    assert "ArcGIS Online" not in result.output


def test_cli_status_lists_rows_due_for_removal(project_tree, monkeypatch) -> None:
    db = project_tree["db"]
    _published_row(db, status="deprecated")
    monkeypatch.setenv("Y2Y_AGOL_AUTO_PUSH", "false")

    result = _invoke(project_tree, ["agol-sync", "status"])

    assert result.exit_code == 0, result.output
    assert "still on AGOL" in result.output
    assert "ds_old" in result.output


def test_cli_tombstone_removes_layer_and_reports(project_tree, monkeypatch) -> None:
    db = project_tree["db"]
    _published_row(db)
    gis, service_item, _ = _fake_gis()
    _auto_push_on(monkeypatch, gis)

    result = _invoke(project_tree, ["tombstone", "ds_old", "--yes"])

    assert result.exit_code == 0, result.output
    assert "removed from ArcGIS Online" in result.output
    service_item.delete.assert_called_once_with(permanent=True)
    fresh = inventory_manager.get_dataset(db, "ds_old")
    assert fresh["status"] == "tombstoned"
    assert fresh["agol_item_id"] is None


def test_cli_tombstone_declined_changes_nothing(project_tree, monkeypatch) -> None:
    db = project_tree["db"]
    _published_row(db)
    gis, service_item, _ = _fake_gis()
    _auto_push_on(monkeypatch, gis)

    result = _invoke(project_tree, ["tombstone", "ds_old"], input="n\n")

    assert result.exit_code != 0
    service_item.delete.assert_not_called()
    assert inventory_manager.get_dataset(db, "ds_old")["status"] == "active"


def test_cli_unpublish_reports_failed_deletion(project_tree, monkeypatch) -> None:
    db = project_tree["db"]
    _published_row(db)
    gis, _, _ = _fake_gis(delete_error=Exception("protected"))
    _auto_push_on(monkeypatch, gis)

    result = _invoke(project_tree, ["agol-sync", "unpublish", "ds_old", "--yes"])

    assert result.exit_code != 0
    assert "unpublish failed" in result.output
    assert inventory_manager.get_dataset(db, "ds_old")["agol_item_id"] == "svc_ds_old"
