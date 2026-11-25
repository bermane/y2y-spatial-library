"""Tests for the Conservation Atlas group audit.

Everything here runs against stub GIS objects; nothing contacts AGOL.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from pipeline import agol_config, agol_group_audit, agol_sync, inventory_manager
from tests.test_agol_push import _full_row

GROUP_ID = "cached_group_xyz"
ORG_ID = "org_y2y"


@pytest.fixture
def _config() -> agol_config.AgolConfig:
    """Config with the group ID cached, so no group search is needed."""
    return agol_config.AgolConfig(conservation_atlas_group_id=GROUP_ID)


def _item(
    item_id: str,
    *,
    title: str = "A layer",
    item_type: str = "Feature Service",
    owner: str = "someone",
    url: str = "https://services1.arcgis.com/abc/arcgis/rest/services/x/FeatureServer",
    keywords: list[str] | None = None,
    org_id: str = ORG_ID,
    license_info: str = "",
    access: str = "org",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=item_id, title=title, type=item_type, owner=owner, url=url,
        typeKeywords=keywords if keywords is not None else ["Hosted Service"],
        orgId=org_id, licenseInfo=license_info, access=access,
        modified=1795000000000,
    )


def _gis(
    group_items: list[Any],
    *,
    other_items: dict[str, Any] | None = None,
) -> MagicMock:
    """A stub GIS whose group holds ``group_items``.

    ``other_items`` are items that exist on AGOL but are not shared to
    the group; ``gis.content.get`` returns None for anything else.
    """
    by_id = {i.id: i for i in group_items}
    by_id.update(other_items or {})
    gis = MagicMock()
    gis.properties.id = ORG_ID
    group = MagicMock()
    group.content.return_value = list(group_items)
    gis.groups.get.return_value = group
    gis.content.get.side_effect = lambda item_id: by_id.get(item_id)
    return gis


def _insert(db, dataset_id: str, **kwargs: Any) -> None:
    status = kwargs.pop("status", "active")
    row = _full_row(
        dataset_id=dataset_id, file_path=f"Water/{dataset_id}.gpkg", **kwargs,
    )
    row["status"] = status
    inventory_manager.insert_dataset(db, row)


# -----------------------------------------------------------------------------
# Group → catalogue
# -----------------------------------------------------------------------------

def test_tracked_item_is_counted_not_flagged(project_tree, _config) -> None:
    db = project_tree["db"]
    _insert(db, "ds_a", agol_item_id="item_a", sync_status="clean")
    gis = _gis([_item("item_a", keywords=["Y2Y", "Y2Y:dataset_id:ds_a"])])

    audit = agol_group_audit.audit_group(db, gis, _config)

    assert audit.error is None
    assert audit.group_item_count == 1
    assert audit.tracked_count == 1
    assert audit.findings_count == 0
    gis.groups.get.assert_called_once_with(GROUP_ID)


def test_untracked_y2y_hosted_layer_is_flagged(project_tree, _config) -> None:
    gis = _gis([_item("item_new", title="Hand-published layer", owner="staff1")])

    audit = agol_group_audit.audit_group(project_tree["db"], gis, _config)

    assert [e.item_id for e in audit.untracked_y2y] == ["item_new"]
    assert audit.untracked_y2y[0].owner == "staff1"
    assert audit.untracked_outside == []


def test_layer_on_outside_server_is_flagged_as_outside_provider(
    project_tree, _config,
) -> None:
    """A layer registered from a URL on a non-AGOL server (e.g. a
    provincial government map server) is an outside-provider layer."""
    gis = _gis([_item(
        "item_bc",
        item_type="Map Service",
        url="https://maps.gov.bc.ca/arcgis/rest/services/whse/MapServer",
        keywords=["Map Service"],
        license_info="Open Government Licence - British Columbia",
    )])

    audit = agol_group_audit.audit_group(project_tree["db"], gis, _config)

    assert [e.item_id for e in audit.untracked_outside] == ["item_bc"]
    assert audit.untracked_outside[0].license_info is True
    assert audit.untracked_y2y == []


def test_layer_owned_by_another_org_is_outside_provider(
    project_tree, _config,
) -> None:
    gis = _gis([_item("item_ext", org_id="org_someone_else")])

    audit = agol_group_audit.audit_group(project_tree["db"], gis, _config)

    assert [e.item_id for e in audit.untracked_outside] == ["item_ext"]


def test_pipeline_stamped_item_without_row_is_an_orphan(
    project_tree, _config,
) -> None:
    """Items carrying the pipeline's keywords, whose dataset this
    catalogue does not know (e.g. published from an earlier catalogue)."""
    gis = _gis([_item(
        "item_old", keywords=["Y2Y", "Y2Y:dataset_id:ds_gone", "Hosted Service"],
    )])

    audit = agol_group_audit.audit_group(project_tree["db"], gis, _config)

    assert [e.item_id for e in audit.pipeline_orphans] == ["item_old"]
    assert audit.pipeline_orphans[0].dataset_id == "ds_gone"
    assert audit.untracked_y2y == []


def test_stamped_item_is_orphan_when_row_links_a_different_item(
    project_tree, _config,
) -> None:
    db = project_tree["db"]
    _insert(db, "ds_a", agol_item_id="item_current", sync_status="clean")
    gis = _gis([
        _item("item_current", keywords=["Y2Y", "Y2Y:dataset_id:ds_a"]),
        _item("item_stale", keywords=["Y2Y", "Y2Y:dataset_id:ds_a"]),
    ])

    audit = agol_group_audit.audit_group(db, gis, _config)

    assert audit.tracked_count == 1
    assert [e.item_id for e in audit.pipeline_orphans] == ["item_stale"]


@pytest.mark.parametrize("status", ["deprecated", "tombstoned"])
def test_item_of_inactive_dataset_is_flagged(
    project_tree, _config, status,
) -> None:
    db = project_tree["db"]
    _insert(db, "ds_old", agol_item_id="item_old", status=status)
    gis = _gis([_item("item_old")])

    audit = agol_group_audit.audit_group(db, gis, _config)

    assert [e.item_id for e in audit.should_be_gone] == ["item_old"]
    assert audit.should_be_gone[0].dataset_id == "ds_old"
    assert status in audit.should_be_gone[0].note
    assert audit.tracked_count == 0
    # An inactive row is never reported as "not in group".
    assert audit.not_in_group == []


def test_non_layer_items_are_counted_by_type_only(project_tree, _config) -> None:
    gis = _gis([
        _item("m1", item_type="Web Map", keywords=[]),
        _item("m2", item_type="Web Map", keywords=[]),
        _item("d1", item_type="Dashboard", keywords=[]),
    ])

    audit = agol_group_audit.audit_group(project_tree["db"], gis, _config)

    assert audit.non_layer_counts == {"Dashboard": 1, "Web Map": 2}
    assert audit.findings_count == 0


# -----------------------------------------------------------------------------
# Catalogue → group
# -----------------------------------------------------------------------------

def test_dataset_on_agol_but_not_in_group_is_flagged(
    project_tree, _config,
) -> None:
    db = project_tree["db"]
    _insert(db, "ds_a", agol_item_id="item_a", sync_status="clean", title="Lakes")
    gis = _gis([], other_items={"item_a": _item("item_a", access="private")})

    audit = agol_group_audit.audit_group(db, gis, _config)

    assert [f.dataset_id for f in audit.not_in_group] == ["ds_a"]
    assert "private" in audit.not_in_group[0].note
    assert audit.missing_on_agol == []


def test_dataset_whose_agol_item_is_gone_is_flagged(
    project_tree, _config,
) -> None:
    db = project_tree["db"]
    _insert(db, "ds_a", agol_item_id="item_deleted", sync_status="clean")
    gis = _gis([])

    audit = agol_group_audit.audit_group(db, gis, _config)

    assert [f.dataset_id for f in audit.missing_on_agol] == ["ds_a"]
    assert audit.not_in_group == []


def test_unpublished_dataset_is_not_flagged(project_tree, _config) -> None:
    db = project_tree["db"]
    _insert(db, "ds_a")  # no agol_item_id
    gis = _gis([])

    audit = agol_group_audit.audit_group(db, gis, _config)

    assert audit.findings_count == 0
    gis.content.get.assert_not_called()


def test_item_lookup_failure_is_reported_not_raised(
    project_tree, _config,
) -> None:
    db = project_tree["db"]
    _insert(db, "ds_a", agol_item_id="item_a", sync_status="clean")
    gis = _gis([])
    gis.content.get.side_effect = ConnectionError("offline")

    audit = agol_group_audit.audit_group(db, gis, _config)

    assert [f.dataset_id for f in audit.unchecked] == ["ds_a"]
    assert audit.not_in_group == [] and audit.missing_on_agol == []


# -----------------------------------------------------------------------------
# Safety: read-only, never raises
# -----------------------------------------------------------------------------

def test_audit_reports_error_when_group_listing_fails(
    project_tree, _config,
) -> None:
    gis = MagicMock()
    gis.groups.get.side_effect = ConnectionError("offline")

    audit = agol_group_audit.audit_group(project_tree["db"], gis, _config)

    assert audit.error is not None and "ConnectionError" in audit.error
    assert audit.findings_count == 0
    assert "did not run" in "\n".join(agol_group_audit.render_markdown(audit))


def test_audit_changes_nothing_in_catalogue_or_on_agol(
    project_tree, _config,
) -> None:
    db = project_tree["db"]
    _insert(db, "ds_a", agol_item_id="item_a", sync_status="clean")
    _insert(db, "ds_b", agol_item_id="item_b", sync_status="clean")
    stray = MagicMock()
    stray.id, stray.title, stray.type = "item_x", "Stray", "Feature Service"
    stray.owner, stray.url, stray.typeKeywords = "o", "", []
    lost = MagicMock()
    lost.access = "org"
    gis = _gis([stray], other_items={"item_a": lost})
    before_rows = inventory_manager.load_inventory(db)
    before_log = inventory_manager.load_changelog(db)

    agol_group_audit.audit_group(db, gis, _config)

    assert inventory_manager.load_inventory(db) == before_rows
    assert inventory_manager.load_changelog(db) == before_log
    for item in (stray, lost):
        item.update.assert_not_called()
        item.delete.assert_not_called()
        item.share.assert_not_called()
        item.sharing.groups.add.assert_not_called()
        item.sharing.groups.remove.assert_not_called()


def test_audit_module_only_lists_the_group() -> None:
    """The audit must never search the whole org; it lists one group."""
    import inspect
    source = inspect.getsource(agol_group_audit)
    assert "content.search" not in source
    assert "users.search" not in source


# -----------------------------------------------------------------------------
# Report + reconcile integration
# -----------------------------------------------------------------------------

def test_render_markdown_lists_every_finding(project_tree, _config) -> None:
    db = project_tree["db"]
    _insert(db, "ds_lost", agol_item_id="item_lost", sync_status="clean")
    _insert(db, "ds_gone", agol_item_id="item_gone", sync_status="clean")
    gis = _gis(
        [
            _item("item_hand", title="Hand layer"),
            _item("item_bc", title="BC layer",
                  url="https://maps.gov.bc.ca/x/MapServer",
                  item_type="Map Service", keywords=[]),
            _item("item_orphan", keywords=["Y2Y", "Y2Y:dataset_id:ds_none"]),
            _item("map1", item_type="Web Map", keywords=[]),
        ],
        other_items={"item_lost": _item("item_lost")},
    )

    text = "\n".join(agol_group_audit.render_markdown(
        agol_group_audit.audit_group(db, gis, _config),
    ))

    assert "## Group audit: Y2Y Conservation Atlas" in text
    for needle in (
        "item_hand", "item_bc", "NO terms of use", "item_orphan",
        "ds_none", "ds_lost", "ds_gone", "Web Map",
    ):
        assert needle in text


def test_render_markdown_says_so_when_everything_agrees(
    project_tree, _config,
) -> None:
    text = "\n".join(agol_group_audit.render_markdown(
        agol_group_audit.audit_group(project_tree["db"], _gis([]), _config),
    ))
    assert "Nothing to review" in text


def test_reconcile_appends_group_audit_to_report(project_tree, _config) -> None:
    db = project_tree["db"]
    gis = _gis([_item("item_new", title="Hand-published layer")])

    report = agol_sync.reconcile_bidirectional(
        db, gis, _config,
        library_root=project_tree["library"], actor="reconcile-cron",
        reports_dir=project_tree["root"] / "reports",
    )

    assert [e.item_id for e in report.group_audit.untracked_y2y] == ["item_new"]
    text = report.report_path.read_text(encoding="utf-8")
    assert "## Group audit" in text
    assert "item_new" in text


def test_reconcile_dry_run_still_runs_the_audit(project_tree, _config) -> None:
    gis = _gis([_item("item_new")])

    report = agol_sync.reconcile_bidirectional(
        project_tree["db"], gis, _config,
        library_root=project_tree["library"], actor="reconcile-cron",
        reports_dir=project_tree["root"] / "reports",
        dry_run=True,
    )

    assert len(report.group_audit.untracked_y2y) == 1


def test_reconcile_completes_when_audit_cannot_run(
    project_tree, _config, monkeypatch,
) -> None:
    """An AGOL failure in the audit must never block the reconcile."""
    db = project_tree["db"]
    _insert(db, "ds_pp", sync_status="pending_push")
    fake = agol_sync.SyncResult(
        dataset_id="ds_pp", action="push",
        sync_status_before="pending_push", sync_status_after="clean",
        agol_item_id="new123", note="published",
    )
    monkeypatch.setattr(agol_sync, "push", MagicMock(return_value=fake))
    gis = MagicMock()
    gis.groups.get.side_effect = ConnectionError("offline")

    report = agol_sync.reconcile_bidirectional(
        db, gis, _config,
        library_root=project_tree["library"], actor="reconcile-cron",
        reports_dir=project_tree["root"] / "reports",
    )

    assert report.counts_by_bucket.get("pushed") == 1
    assert report.group_audit.error is not None
    assert "did not run" in report.report_path.read_text(encoding="utf-8")


# -----------------------------------------------------------------------------
# _apply_sharing: a failed group share is reported, not swallowed
# -----------------------------------------------------------------------------

def test_apply_sharing_returns_warning_when_group_share_fails() -> None:
    item = MagicMock()
    item.sharing.sharing_level = "PRIVATE"
    item.sharing.groups.add.side_effect = RuntimeError("no permission")

    warnings = agol_sync._apply_sharing(
        item, {"everyone": False, "org": True, "groups": [GROUP_ID]},
    )

    assert item.sharing.sharing_level == "ORGANIZATION"
    assert len(warnings) == 1
    assert GROUP_ID in warnings[0] and "no permission" in warnings[0]


def test_apply_sharing_returns_no_warning_on_success() -> None:
    item = MagicMock()
    item.sharing.sharing_level = "PRIVATE"

    warnings = agol_sync._apply_sharing(
        item, {"everyone": False, "org": True, "groups": [GROUP_ID]},
    )

    assert warnings == []
    item.sharing.groups.add.assert_called_once_with(group=GROUP_ID)
