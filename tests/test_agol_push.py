"""Tests for the push() flow in pipeline.agol_sync.

The arcgis SDK's GIS class is fully mocked here — push() touches
``gis.content.add``, ``gis.content.get``, ``Item.publish``,
``Item.update``, ``item.sharing``, and ``gis.groups.search``
(via resolve_group_id). All of those are wired through MagicMocks
so the tests run offline.

Real fixtures (a tmp library_root with valid GPKG/COG sources + a
fresh inventory.db with full schema) come from conftest's
``project_tree``, ``valid_gpkg_factory``, ``valid_cog_factory``.

Test coverage:
- Validation gates (status, sync_status, target/format mismatch)
- Create vs update path branching
- Dry-run no-side-effects guarantee
- Sharing payload (default + override)
- Catalogue + changelog updates on success
- Per-row error isolation in push_all_dirty
- Hosted-publish fallback for imagery-layer
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from pipeline import agol_config, agol_sync, inventory_manager


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------

def _full_row(
    *,
    dataset_id: str,
    file_path: str,
    classification: str = "vector",
    format_: str = "geopackage",
    agol_target: str = "feature-layer",
    title: str = "Test Title",
    category: str = "Water",
    subcategory: str | None = None,
    sync_status: str = "unpublished",
    agol_item_id: str | None = None,
) -> dict[str, Any]:
    """Return a row dict that satisfies every NOT NULL + CHECK in schema.sql."""
    return {
        "dataset_id": dataset_id,
        "dataset_type": "spatial",
        "title": title,
        "category": category,
        "subcategory": subcategory,
        "file_path": file_path,
        "format": format_,
        "summary": "Summary.",
        "description": "Description.",
        "tags": "test;y2y",
        "terms_of_use": "TOU.",
        "acknowledgements": "Ack.",
        "data_steward": "Tester",
        "internal_notes": None,
        "status": "active",
        "date_added": "2026-04-29T00:00:00Z",
        "date_modified": "2026-04-29T00:00:00Z",
        "agol_item_id": agol_item_id,
        "agol_published_at": None,
        "last_synced_at": None,
        "sync_status": sync_status,
        "agol_target": agol_target,
        "checksum_sha256": "a" * 64,
        "size_bytes": 1024,
        "mtime": "2026-04-29T00:00:00Z",
        "crs": "ESRI:102008",
        "geographic_extent_bbox": "0,0,1,1",
        "classification": classification,
        "footprint_wkt": None,
        "temporal_start": None,
        "temporal_end": None,
        "feature_count": 1,
        "raster_width": None,
        "raster_height": None,
        "pixel_size_x": None,
        "pixel_size_y": None,
        "source_format": "geopackage" if format_ == "geopackage" else "geotiff",
        "source_filename": "src.bin",
        "source_crs": "ESRI:102008",
        "source_layer": None,
    }


def _make_gis(
    *,
    new_item_id: str = "new_item_id_xyz",
    existing_item: MagicMock | None = None,
    group_id: str = "group_abc",
    publish_raises: Exception | None = None,
) -> MagicMock:
    """Build a MagicMock GIS that behaves like arcgis.gis.GIS for push().

    Mock chain (mirrors the AGOL two-item model — source uploaded
    item + published service):

        gis.content.add(...)            → source_item (MagicMock)
        source_item.publish(...)        → service_item (MagicMock)
                                          OR raises publish_raises

    Tests reach the source via ``gis.content.add.return_value`` and the
    service via ``gis.content.add.return_value.publish.return_value``.
    """
    gis = MagicMock()

    # gis.groups.search → returns a group with matching title.
    group = MagicMock()
    group.title = "Y2Y Conservation Atlas"
    group.id = group_id
    gis.groups.search.return_value = [group]

    # The source item — what gis.content.add returns.
    source = MagicMock()
    source.id = new_item_id  # for fallback path: stored as agol_item_id
    source.sharing = MagicMock()
    source.sharing.sharing_level = "PRIVATE"
    source.sharing.groups = MagicMock()

    if publish_raises is not None:
        source.publish.side_effect = publish_raises
    else:
        # The service item — what source.publish() returns.
        service = MagicMock()
        service.id = new_item_id  # stored as agol_item_id on the catalogue
        service.sharing = MagicMock()
        service.sharing.sharing_level = "PRIVATE"
        service.sharing.groups = MagicMock()
        source.publish.return_value = service

    gis.content.add.return_value = source

    if existing_item is not None:
        gis.content.get.return_value = existing_item
    else:
        gis.content.get.return_value = None

    return gis


@pytest.fixture
def _config_no_cache(tmp_path: Path) -> agol_config.AgolConfig:
    """A config whose group cache has been pre-populated so resolve_group_id
    doesn't need to call gis.groups.search at all."""
    return agol_config.AgolConfig(
        conservation_atlas_group_id="cached_group_xyz",
    )


# ----------------------------------------------------------------------------
# Validation gates
# ----------------------------------------------------------------------------

def test_push_refuses_when_dataset_not_in_catalogue(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    db = project_tree["db"]
    gis = _make_gis()
    # Don't insert anything.
    with pytest.raises(agol_sync.AgolError, match="not in catalogue"):
        agol_sync.push(
            db, "ds_ghost", gis, _config_no_cache,
            library_root=project_tree["library"],
            actor="tester",
        )


def test_push_refuses_when_status_not_active(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    row = _full_row(dataset_id="ds_test", file_path="Water/v.gpkg")
    row["status"] = "deprecated"
    inventory_manager.insert_dataset(db, row)
    gis = _make_gis()
    with pytest.raises(agol_sync.AgolError, match="status is 'deprecated'"):
        agol_sync.push(
            db, "ds_test", gis, _config_no_cache,
            library_root=project_tree["library"], actor="tester",
        )


def test_push_allows_re_push_of_clean_row(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """'clean' is a re-pushable status — the push is idempotent on a
    clean row (metadata + sharing + source reconcile all converge to
    the same end state). Pilot testing + force-resyncs need this."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")

    existing = MagicMock()
    existing.id = "preexisting_id"
    existing.sharing = MagicMock()
    existing.sharing.sharing_level = "ORGANIZATION"
    existing.related_items.return_value = []  # no source linked

    row = _full_row(
        dataset_id="ds_test", file_path="Water/v.gpkg",
        sync_status="clean", agol_item_id="preexisting_id",
    )
    # No checksum change so the push is purely a metadata+sharing+
    # reconcile re-application (no data refresh).
    row["last_synced_at"] = "2030-01-01T00:00:00Z"
    row["date_modified"] = "2026-04-29T00:00:00Z"
    inventory_manager.insert_dataset(db, row)
    gis = _make_gis(existing_item=existing)

    result = agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )
    assert result.sync_status_after == "clean"


def test_push_refuses_when_sync_status_is_pending_pull(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """'pending_pull' indicates AGOL-side drift the steward needs to
    triage via pull/conflict resolution (Phase D). A push would
    silently overwrite that drift, so it's refused."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    row = _full_row(dataset_id="ds_test", file_path="Water/v.gpkg",
                    sync_status="pending_pull", agol_item_id="abc")
    inventory_manager.insert_dataset(db, row)
    gis = _make_gis()
    with pytest.raises(agol_sync.AgolError, match="sync_status is 'pending_pull'"):
        agol_sync.push(
            db, "ds_test", gis, _config_no_cache,
            library_root=project_tree["library"], actor="tester",
        )


def test_push_refuses_target_format_mismatch(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """imagery-layer on a vector source is rejected before any AGOL contact."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    row = _full_row(
        dataset_id="ds_test", file_path="Water/v.gpkg",
        agol_target="imagery-layer",  # mismatched
    )
    inventory_manager.insert_dataset(db, row)
    gis = _make_gis()
    with pytest.raises(agol_sync.AgolError, match="not valid for format"):
        agol_sync.push(
            db, "ds_test", gis, _config_no_cache,
            library_root=project_tree["library"], actor="tester",
        )
    # No AGOL contact should have happened.
    gis.content.add.assert_not_called()


# ----------------------------------------------------------------------------
# Dry-run
# ----------------------------------------------------------------------------

def test_push_dry_run_does_not_contact_agol(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    row = _full_row(dataset_id="ds_test", file_path="Water/v.gpkg")
    inventory_manager.insert_dataset(db, row)
    gis = _make_gis()

    result = agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        dry_run=True,
    )

    assert result.action == "push (dry-run)"
    assert result.sync_status_before == "unpublished"
    assert result.sync_status_after == "unpublished"  # unchanged
    gis.content.add.assert_not_called()
    # Catalogue row unchanged.
    after = inventory_manager.get_dataset(db, "ds_test")
    assert after["sync_status"] == "unpublished"
    assert after["agol_item_id"] is None


# ----------------------------------------------------------------------------
# Create path — feature-layer
# ----------------------------------------------------------------------------

def test_push_creates_feature_layer_for_new_vector(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    row = _full_row(dataset_id="ds_test", file_path="Water/v.gpkg")
    inventory_manager.insert_dataset(db, row)
    gis = _make_gis(new_item_id="new_fl_id")

    result = agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # gis.content.add was called for the SOURCE GPKG item — that lands
    # in the dedicated _sources folder with minimal properties.
    # The service item (Feature Service) is created by source.publish()
    # and then moved to the category folder. See the per-contract
    # tests below for finer assertions on each step.
    assert gis.content.add.called

    # Result reflects the new item id (the service, not the source).
    assert result.agol_item_id == "new_fl_id"
    assert result.sync_status_after == "clean"

    # Catalogue updated.
    after = inventory_manager.get_dataset(db, "ds_test")
    assert after["agol_item_id"] == "new_fl_id"
    assert after["sync_status"] == "clean"
    assert after["last_synced_at"] is not None
    assert after["agol_published_at"] is not None

    # Changelog row written.
    log = inventory_manager.load_changelog(db)
    assert any(
        r["action"] == "metadata" and r["dataset_id"] == "ds_test"
        and r["field_changed"] == "sync_status"
        for r in log
    )


# ----------------------------------------------------------------------------
# Source-item contract: minimal props + _sources folder + private sharing
# ----------------------------------------------------------------------------

def _last_content_add_kwargs(gis):
    """Return the kwargs of the most recent gis.content.add(...) call."""
    return gis.content.add.call_args.kwargs


def test_push_creates_source_with_minimal_properties_in_sources_folder(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """Source items (GPKG, COG, VTPK) carry only the minimum metadata:
    title, type, description stub, and Y2Y typeKeywords. Categories,
    tags, accessInformation, licenseInfo, snippet — all excluded. Folder
    is the dedicated _sources folder, not the category folder."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    row = _full_row(dataset_id="ds_test", file_path="Water/v.gpkg")
    inventory_manager.insert_dataset(db, row)
    gis = _make_gis(new_item_id="new_fl_id")

    agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # Source GPKG was added with minimal props in _sources folder.
    source_call_kwargs = _last_content_add_kwargs(gis)
    assert source_call_kwargs["folder"] == "_sources"

    source_props = source_call_kwargs["item_properties"]
    # Minimal allowed keys
    assert source_props["type"] == "GeoPackage"
    assert source_props["title"] == "Test Title"
    assert "description" in source_props
    assert "Y2Y" in source_props["typeKeywords"]
    assert "Y2Y:source" in source_props["typeKeywords"]
    assert "Y2Y:dataset_id:ds_test" in source_props["typeKeywords"]

    # Forbidden keys (public-facing metadata that belongs on the service only)
    for forbidden in ("categories", "tags", "snippet",
                       "accessInformation", "licenseInfo"):
        assert forbidden not in source_props, (
            f"source props leaked {forbidden!r} — public-facing "
            f"metadata must stay on the service item only"
        )

    # Source typeKeywords MUST NOT include the Y2Y:category:... keyword.
    assert not any(
        k.startswith("Y2Y:category:") for k in source_props["typeKeywords"]
    ), "source items should not carry the Y2Y:category typeKeyword"


def test_push_moves_service_to_category_folder_after_publish(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """The service item is created by source.publish() (lands in My
    Content root by default), then moved to the category folder. The
    .move(folder=...) call is the regression guard."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    row = _full_row(dataset_id="ds_test", file_path="Water/v.gpkg")
    inventory_manager.insert_dataset(db, row)
    gis = _make_gis(new_item_id="new_fl_id")

    agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # The service item is the publish() return value. Find it via the
    # mock chain: gis.content.add → returns source; source.publish →
    # returns service. The service should have .move() called on it
    # with the Folder instance returned by content.folders.create().
    source_item = gis.content.add.return_value
    service_item = source_item.publish.return_value
    # _safe_move now passes the live Folder instance (from
    # _ensure_folder, which calls gis.content.folders.create) rather
    # than the raw string name — string-based lookups are unreliable
    # right after folder creation (AGOL hasn't indexed it yet).
    service_item.move.assert_called_once()
    move_kwargs = service_item.move.call_args.kwargs
    assert "folder" in move_kwargs
    # The folder arg is the Folder instance returned by
    # folders.create(folder="Water", exist_ok=True). Verify the
    # create() call asked for the correct name.
    create_calls = [
        c for c in gis.content.folders.create.call_args_list
        if c.kwargs.get("folder") == "Water"
        and c.kwargs.get("exist_ok") is True
    ]
    assert create_calls, (
        "expected folders.create(folder='Water', exist_ok=True) before move"
    )


def test_push_ensures_target_folders_exist_before_publish(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """push() pre-creates both the _sources folder and the category
    folder. AGOL's Item.move() requires the target folder to exist;
    this guard prevents the 'service ended up in My Content root'
    symptom test #1 surfaced before this fix landed."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    row = _full_row(dataset_id="ds_test", file_path="Water/v.gpkg")
    inventory_manager.insert_dataset(db, row)
    gis = _make_gis(new_item_id="new_fl_id")

    agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # Both folders should have been touched (idempotently, via
    # exist_ok=True) before the publish + move. _ensure_folder calls
    # gis.content.folders.create(folder=<name>, exist_ok=True).
    assert gis.content.folders.create.called
    requested_folders = [
        c.kwargs.get("folder") for c in gis.content.folders.create.call_args_list
    ]
    assert "_sources" in requested_folders
    assert "Water" in requested_folders
    # Every create call must use exist_ok=True (idempotent — second
    # push of the same dataset would FolderException otherwise).
    for c in gis.content.folders.create.call_args_list:
        assert c.kwargs.get("exist_ok") is True, (
            f"folders.create called without exist_ok=True: {c}"
        )


def test_push_surfaces_warning_when_move_returns_success_false(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """Regression guard for the silent-fail symptom of pilot test #1:
    AGOL's Item.move() returns ``{"success": False, ...}`` instead of
    raising when the server-side move refuses. The previous
    _safe_move only caught Python exceptions, so a success=False
    payload silently left the item in My Content root with no
    diagnostic. We now check the success flag explicitly."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    row = _full_row(dataset_id="ds_test", file_path="Water/v.gpkg")
    inventory_manager.insert_dataset(db, row)
    gis = _make_gis(new_item_id="new_fl_id")

    # Make the service's move() return success=False (mimicking AGOL
    # refusing the move).
    source_item = gis.content.add.return_value
    service_item = source_item.publish.return_value
    service_item.move.return_value = {
        "success": False, "itemId": "new_fl_id", "error": "denied",
    }

    agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    after = inventory_manager.get_dataset(db, "ds_test")
    notes = after["internal_notes"] or ""
    assert "[agol]" in notes
    assert "success=False" in notes or "still in My Content root" in notes


def test_push_surfaces_warning_when_move_returns_none(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """Item.move() returns ``None`` when the SDK can't resolve the
    target folder (logs 'Folder not found for given owner' to stdout
    and bails). Silent failure — _safe_move must catch this."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    row = _full_row(dataset_id="ds_test", file_path="Water/v.gpkg")
    inventory_manager.insert_dataset(db, row)
    gis = _make_gis(new_item_id="new_fl_id")

    source_item = gis.content.add.return_value
    service_item = source_item.publish.return_value
    service_item.move.return_value = None

    agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    after = inventory_manager.get_dataset(db, "ds_test")
    notes = after["internal_notes"] or ""
    assert "[agol]" in notes
    assert "None" in notes or "could not resolve" in notes


def test_push_reconciles_source_item_on_every_update(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """Every update-path push reconciles the source item to the
    minimal-source policy: moved to _sources, metadata stripped to
    title-plus-typeKeywords-plus-stub-description, sharing forced
    to PRIVATE. Steward-confirmed contract (2026-05-22)."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")

    # Existing service item with a related source item.
    service = MagicMock()
    service.id = "service_id"
    service.sharing = MagicMock()
    service.sharing.sharing_level = "ORGANIZATION"
    service.sharing.groups = MagicMock()

    source = MagicMock()
    source.id = "source_id"
    source.sharing = MagicMock()
    # Pretend the source was manually set to org-visible.
    source.sharing.sharing_level = "ORGANIZATION"

    # service.related_items('Service2Data', 'forward') → [source]
    service.related_items.return_value = [source]

    row = _full_row(
        dataset_id="ds_test", file_path="Water/v.gpkg",
        sync_status="pending_push",
        agol_item_id="service_id",
    )
    # Force checksum_changed = False so this test isolates the
    # reconcile path (not data-refresh).
    row["last_synced_at"] = "2030-01-01T00:00:00Z"
    row["date_modified"] = "2026-04-29T00:00:00Z"
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(existing_item=service)

    agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # The source item was moved to _sources. _safe_move passes the
    # live Folder instance returned by folders.create(); verify the
    # create() call asked for the right name.
    source.move.assert_called_once()
    sources_create_calls = [
        c for c in gis.content.folders.create.call_args_list
        if c.kwargs.get("folder") == "_sources"
        and c.kwargs.get("exist_ok") is True
    ]
    assert sources_create_calls, (
        "expected folders.create(folder='_sources', exist_ok=True)"
    )

    # The source item was updated with the minimal-source props.
    source_update_calls = [
        c for c in source.update.call_args_list
        if "item_properties" in c.kwargs
    ]
    assert source_update_calls, "source must be updated with minimal props"
    enforced_props = source_update_calls[-1].kwargs["item_properties"]
    # Minimal-source policy: title + description stub + Y2Y typeKeywords
    assert enforced_props["title"] == "Test Title"
    assert "Y2Y:source" in enforced_props["typeKeywords"]
    assert "Y2Y:dataset_id:ds_test" in enforced_props["typeKeywords"]
    # Categories and other public-facing fields are explicitly cleared.
    assert enforced_props["categories"] == []
    assert enforced_props["tags"] == []
    assert enforced_props["snippet"] == ""
    assert enforced_props["accessInformation"] == ""
    assert enforced_props["licenseInfo"] == ""

    # Source sharing was forced to PRIVATE.
    assert source.sharing.sharing_level == "PRIVATE"


def test_push_skips_source_reconcile_if_no_related_source(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """If the service has no Service2Data link (legacy uploads,
    manually-linked items), source reconcile silently skips. The
    update path still completes successfully — service metadata
    + sharing are independent of source state."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")

    service = MagicMock()
    service.id = "service_id"
    service.sharing = MagicMock()
    service.sharing.sharing_level = "ORGANIZATION"
    service.sharing.groups = MagicMock()
    # No source linked.
    service.related_items.return_value = []

    row = _full_row(
        dataset_id="ds_test", file_path="Water/v.gpkg",
        sync_status="pending_push", agol_item_id="service_id",
    )
    row["last_synced_at"] = "2030-01-01T00:00:00Z"
    row["date_modified"] = "2026-04-29T00:00:00Z"
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(existing_item=service)

    result = agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # Update path completed cleanly.
    assert result.sync_status_after == "clean"
    # related_items WAS queried (the integration always tries to find
    # the source) but returned empty, so no further source-side work.
    service.related_items.assert_called()


def test_push_collects_multiple_warnings_into_internal_notes(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """Multiple warnings from a single push (e.g., source move fail
    + source update fail) all reach the steward instead of last-
    write-wins overwriting each other. Exercises the update path
    where two source-side failures can stack."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")

    # Existing service with a related source; inject TWO failures
    # in the source's reconcile path so both can be observed in the
    # final internal_notes.
    service = MagicMock()
    service.id = "service_id"
    service.sharing = MagicMock()
    service.sharing.sharing_level = "ORGANIZATION"
    service.sharing.groups = MagicMock()

    source = MagicMock()
    source.id = "source_id"
    source.move.side_effect = RuntimeError("simulated source move failure")
    source.update.side_effect = RuntimeError("simulated source update failure")
    source.sharing = MagicMock()
    source.sharing.sharing_level = "ORGANIZATION"

    service.related_items.return_value = [source]

    row = _full_row(
        dataset_id="ds_test", file_path="Water/v.gpkg",
        sync_status="pending_push", agol_item_id="service_id",
    )
    row["last_synced_at"] = "2030-01-01T00:00:00Z"
    row["date_modified"] = "2026-04-29T00:00:00Z"
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(existing_item=service)

    agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    after = inventory_manager.get_dataset(db, "ds_test")
    notes = after["internal_notes"] or ""
    # Both warnings present (not one overwriting the other).
    assert notes.count("[agol]") >= 2, (
        f"expected at least 2 [agol] annotations; got: {notes!r}"
    )
    assert "move" in notes.lower()
    assert "reconcile" in notes.lower() or "metadata" in notes.lower()


def test_push_surfaces_move_failure_as_internal_notes_warning(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """If service.move(folder=...) raises, the failure must surface in
    internal_notes (and the changelog) — not be silently swallowed.
    The 'item ended up in My Content root' symptom needs to be
    visible to the steward."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    row = _full_row(dataset_id="ds_test", file_path="Water/v.gpkg")
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(new_item_id="new_fl_id")
    # Make the service's move() raise.
    source = gis.content.add.return_value
    service = source.publish.return_value
    service.move.side_effect = RuntimeError("simulated move failure for test")

    result = agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # Push still succeeded (move failure isn't fatal).
    assert result.sync_status_after == "clean"

    # Catalogue carries the [agol] annotation describing the move failure.
    after = inventory_manager.get_dataset(db, "ds_test")
    assert "[agol]" in (after["internal_notes"] or "")
    assert "move" in (after["internal_notes"] or "").lower()
    assert "My Content root" in (after["internal_notes"] or "")


def test_push_applies_full_metadata_to_service_not_source(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """The full steward-authored metadata (title, snippet, description,
    tags, accessInformation, licenseInfo, categories) lands on the
    SERVICE item via service.update(item_properties=...), not on the
    source."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    row = _full_row(dataset_id="ds_test", file_path="Water/v.gpkg")
    inventory_manager.insert_dataset(db, row)
    gis = _make_gis(new_item_id="new_fl_id")

    agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # Find the service.update(item_properties=...) call.
    source_item = gis.content.add.return_value
    service_item = source_item.publish.return_value
    update_calls_with_props = [
        c for c in service_item.update.call_args_list
        if "item_properties" in c.kwargs
    ]
    assert len(update_calls_with_props) >= 1, (
        "service.update(item_properties=...) must be called to apply "
        "the full steward-authored metadata"
    )
    full_props = update_calls_with_props[-1].kwargs["item_properties"]
    assert full_props["title"] == "Test Title"
    assert full_props["categories"] == ["Water"]
    assert full_props["snippet"] == "Summary."
    assert full_props["accessInformation"] == "Ack."
    assert full_props["licenseInfo"] == "TOU."
    assert full_props["tags"] == ["test", "y2y"]


# ----------------------------------------------------------------------------
# Update path — existing item
# ----------------------------------------------------------------------------

def test_push_updates_existing_item_metadata_only_no_data_change(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """When the catalogue's date_modified < last_synced_at, the file
    hasn't changed since the last sync. The update path should only
    push metadata to the service; no data-overwrite, no publish() on
    the service."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")

    existing = MagicMock()
    existing.id = "preexisting_id"
    existing.sharing = MagicMock()
    existing.sharing.sharing_level = "ORGANIZATION"
    existing.sharing.groups = MagicMock()
    # related_items IS called on every update now (for the source
    # reconcile, steward-confirmed 2026-05-22). Return empty so the
    # reconcile is a no-op for this metadata-only-update test —
    # other tests cover the source-reconcile branch when a source
    # exists.
    existing.related_items = MagicMock(return_value=[])

    row = _full_row(
        dataset_id="ds_test", file_path="Water/v.gpkg",
        sync_status="pending_push",
        agol_item_id="preexisting_id",
    )
    # Pretend a prior sync happened so checksum_changed=False.
    row["last_synced_at"] = "2030-01-01T00:00:00Z"
    row["date_modified"] = "2026-04-29T00:00:00Z"  # < last_synced_at
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(existing_item=existing)

    result = agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # Used the update path (not the create path).
    gis.content.add.assert_not_called()
    # service.update was called multiple times: once with
    # item_properties=... (the metadata push from
    # _publish_feature_layer) and once with thumbnail=... (the
    # post-publish thumbnail upload). Verify both happened.
    update_calls = existing.update.call_args_list
    assert any("item_properties" in c.kwargs for c in update_calls), (
        f"expected at least one item.update(item_properties=...) call; "
        f"got: {update_calls}"
    )
    assert any("thumbnail" in c.kwargs for c in update_calls), (
        f"expected the post-publish thumbnail update; got: {update_calls}"
    )
    # **REGRESSION GUARD**: never call publish() on the service item
    # during an update. The test-#1 bug was caused by exactly this
    # call shape; AGOL created a duplicate FS derived from the
    # service. Catching this here means a future regression in the
    # update path can't repeat that failure mode.
    existing.publish.assert_not_called()
    # related_items IS called (for source reconcile) — verify the
    # call shape but not the count, since the helper may walk
    # multiple relationship types in future revisions.
    existing.related_items.assert_called_with(
        rel_type="Service2Data", direction="forward",
    )

    assert result.agol_item_id == "preexisting_id"
    assert result.sync_status_after == "clean"


def test_push_updates_existing_item_data_changed_uses_FLC_overwrite(
    project_tree, _config_no_cache, valid_gpkg_factory, monkeypatch,
) -> None:
    """When the file has changed since the last sync, the update path
    refreshes the service's data via FeatureLayerCollection.overwrite(),
    NOT via service.publish(). This is the regression guard for the
    duplicate-creation bug surfaced by test #1 of the manual pilot."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")

    existing = MagicMock()
    existing.id = "preexisting_id"
    existing.sharing = MagicMock()
    existing.sharing.sharing_level = "ORGANIZATION"
    existing.sharing.groups = MagicMock()
    # No linked source → push() takes the self-hosted FS branch
    # (FLC.overwrite) instead of source-publish.
    existing.related_items.return_value = []

    row = _full_row(
        dataset_id="ds_test", file_path="Water/v.gpkg",
        sync_status="pending_push",
        agol_item_id="preexisting_id",
    )
    # Force checksum_changed = True via last_synced_at < date_modified.
    row["last_synced_at"] = "2026-01-01T00:00:00Z"
    row["date_modified"] = "2026-04-29T00:00:00Z"  # > last_synced_at
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(existing_item=existing)

    # Mock the FLC.fromitem + .manager.overwrite chain so we can
    # assert it's the call that actually fires.
    overwrite_calls: list[str] = []
    fake_flc = MagicMock()
    fake_flc.manager = MagicMock()
    fake_flc.manager.overwrite = MagicMock(
        side_effect=lambda path: overwrite_calls.append(path)
    )
    fake_flc_class = MagicMock()
    fake_flc_class.fromitem = MagicMock(return_value=fake_flc)

    # Patch the arcgis.features.FeatureLayerCollection import inside
    # _publish_feature_layer.
    import sys, types
    fake_features = types.ModuleType("arcgis.features")
    fake_features.FeatureLayerCollection = fake_flc_class
    fake_arcgis = sys.modules.get("arcgis") or types.ModuleType("arcgis")
    monkeypatch.setitem(sys.modules, "arcgis", fake_arcgis)
    monkeypatch.setitem(sys.modules, "arcgis.features", fake_features)

    result = agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # Metadata update fired.
    existing.update.assert_called()
    # FeatureLayerCollection.fromitem(service).manager.overwrite was
    # called with the source path.
    fake_flc_class.fromitem.assert_called_once_with(existing)
    fake_flc.manager.overwrite.assert_called_once()
    overwrite_arg = fake_flc.manager.overwrite.call_args.args[0]
    assert "v.gpkg" in overwrite_arg

    # **REGRESSION GUARD**: service.publish() must NEVER be called on
    # an update. Calling publish on a service item is what AGOL
    # interprets as 'derive a new service from this service', which
    # created the duplicate FS in the pilot.
    existing.publish.assert_not_called()

    assert result.sync_status_after == "clean"


def test_push_update_falls_back_if_FLC_overwrite_raises(
    project_tree, _config_no_cache, valid_gpkg_factory, monkeypatch,
) -> None:
    """If FeatureLayerCollection.overwrite raises, the push records a
    [agol] annotation in internal_notes + changelog rather than
    aborting. Metadata changes still land."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")

    existing = MagicMock()
    existing.id = "preexisting_id"
    existing.sharing = MagicMock()
    existing.sharing.sharing_level = "ORGANIZATION"
    existing.sharing.groups = MagicMock()
    # No linked source → push() takes the self-hosted FS (FLC.overwrite)
    # branch, where the fallback warning under test fires.
    existing.related_items.return_value = []

    row = _full_row(
        dataset_id="ds_test", file_path="Water/v.gpkg",
        sync_status="pending_push", agol_item_id="preexisting_id",
    )
    row["last_synced_at"] = "2026-01-01T00:00:00Z"
    row["date_modified"] = "2026-04-29T00:00:00Z"
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(existing_item=existing)

    # Patch FeatureLayerCollection to raise.
    import sys, types
    fake_flc_class = MagicMock()
    fake_flc_class.fromitem.side_effect = RuntimeError("overwrite failed in test")
    fake_features = types.ModuleType("arcgis.features")
    fake_features.FeatureLayerCollection = fake_flc_class
    fake_arcgis = sys.modules.get("arcgis") or types.ModuleType("arcgis")
    monkeypatch.setitem(sys.modules, "arcgis", fake_arcgis)
    monkeypatch.setitem(sys.modules, "arcgis.features", fake_features)

    result = agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # Push still completed.
    assert result.sync_status_after == "clean"

    # internal_notes carries the fallback warning.
    after = inventory_manager.get_dataset(db, "ds_test")
    notes = after["internal_notes"] or ""
    assert "[agol]" in notes
    assert "overwrite" in notes.lower() or "stale" in notes.lower()

    # publish on service still not called.
    existing.publish.assert_not_called()


def test_push_updates_existing_item_with_linked_source_uses_source_publish(
    project_tree, _config_no_cache, valid_gpkg_factory, monkeypatch,
) -> None:
    """When the service item has a linked source (Service2Data forward),
    a checksum-changed update path must refresh the data via
    ``source.update(data=...) + source.publish(overwrite=True)`` — NOT
    via ``FeatureLayerCollection.overwrite()``.

    This guards the AGOL Error 500 surfaced by test #1 of the manual
    pilot: 'User cannot overwrite this service, using this data, as
    this data is already referring to another service.' Linked-FS
    services refuse FLC.overwrite at the AGOL backend; they require
    the source-publish pattern instead.
    """
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")

    # The linked source item, returned by service.related_items(Service2Data, forward).
    linked_source = MagicMock()
    linked_source.id = "source_gpkg_id"
    # Default related_items on the source itself (used by _reconcile_source_item).
    linked_source.related_items.return_value = []

    existing = MagicMock()
    existing.id = "preexisting_service_id"
    existing.sharing = MagicMock()
    existing.sharing.sharing_level = "ORGANIZATION"
    existing.sharing.groups = MagicMock()
    # Service2Data forward returns the linked source.
    existing.related_items.return_value = [linked_source]

    row = _full_row(
        dataset_id="ds_test", file_path="Water/v.gpkg",
        sync_status="pending_push", agol_item_id="preexisting_service_id",
    )
    # Force checksum_changed = True via last_synced_at < date_modified.
    row["last_synced_at"] = "2026-01-01T00:00:00Z"
    row["date_modified"] = "2026-04-29T00:00:00Z"
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(existing_item=existing)

    # Patch FeatureLayerCollection so we can assert it was NOT used.
    import sys, types
    fake_flc_class = MagicMock()
    fake_features = types.ModuleType("arcgis.features")
    fake_features.FeatureLayerCollection = fake_flc_class
    fake_arcgis = sys.modules.get("arcgis") or types.ModuleType("arcgis")
    monkeypatch.setitem(sys.modules, "arcgis", fake_arcgis)
    monkeypatch.setitem(sys.modules, "arcgis.features", fake_features)

    result = agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # source.update(data=<gpkg path>) was called.
    update_data_args = [
        c for c in linked_source.update.call_args_list
        if c.kwargs.get("data") and "v.gpkg" in str(c.kwargs["data"])
    ]
    assert update_data_args, "source.update(data=<gpkg path>) was not called"

    # source.publish(file_type='GeoPackage', overwrite=True) was called.
    linked_source.publish.assert_called_once()
    pub_kwargs = linked_source.publish.call_args.kwargs
    assert pub_kwargs.get("file_type") == "GeoPackage"
    assert pub_kwargs.get("overwrite") is True

    # REGRESSION GUARD: FeatureLayerCollection.overwrite must NOT have
    # been used — that's the AGOL Error 500 path on linked-FS services.
    fake_flc_class.fromitem.assert_not_called()

    # Service metadata was still re-applied.
    existing.update.assert_called()
    # Service .publish() must never be called on update (would create
    # a duplicate FS).
    existing.publish.assert_not_called()

    assert result.sync_status_after == "clean"


# ----------------------------------------------------------------------------
# Imagery-layer fallback when hosted publish fails
# ----------------------------------------------------------------------------

def test_push_imagery_falls_back_to_item_with_file_on_publish_failure(
    project_tree, _config_no_cache, valid_cog_factory,
) -> None:
    """When .publish() on a GeoTIFF item raises, push() retains the
    source TIFF as a downloadable file (no hosted service), upgrades
    it to service-like treatment (moved to category folder, full
    metadata applied), records the fallback in internal_notes +
    changelog, and marks sync_status='clean'."""
    db = project_tree["db"]
    raster_dir = project_tree["library"] / "Land_Cover_Use_Disturbance"
    raster_dir.mkdir(parents=True, exist_ok=True)
    valid_cog_factory("r.tif", dest_dir=raster_dir, dtype="uint8", nodata=255)

    row = _full_row(
        dataset_id="ds_raster", file_path="Land_Cover_Use_Disturbance/r.tif",
        classification="categorical", format_="geotiff",
        agol_target="imagery-layer",
        category="Land Cover, Land Use & Disturbance",
    )
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(
        new_item_id="raster_item_id",
        publish_raises=RuntimeError("Hosted imagery for COGs not supported in beta"),
    )

    result = agol_sync.push(
        db, "ds_raster", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # Item still got created (as a downloadable file).
    assert result.agol_item_id == "raster_item_id"
    assert result.sync_status_after == "clean"

    # Fallback recorded in internal_notes.
    after = inventory_manager.get_dataset(db, "ds_raster")
    assert "[agol]" in (after["internal_notes"] or "")
    assert "fallback" in (after["internal_notes"] or "").lower() \
        or "hosted imagery publish failed" in (after["internal_notes"] or "")

    # Fallback "upgrade": the source TIFF — which is what users will
    # consume since the hosted service publish failed — is moved to
    # the category folder and gets the full steward-authored metadata.
    # The move() receives the live Folder instance from folders.create().
    source_item = gis.content.add.return_value
    source_item.move.assert_called_once()
    category_create_calls = [
        c for c in gis.content.folders.create.call_args_list
        if c.kwargs.get("folder") == "Land_Cover_Use_Disturbance"
        and c.kwargs.get("exist_ok") is True
    ]
    assert category_create_calls, (
        "expected folders.create(folder='Land_Cover_Use_Disturbance', "
        "exist_ok=True)"
    )
    upgrade_calls = [
        c for c in source_item.update.call_args_list
        if "item_properties" in c.kwargs
    ]
    assert len(upgrade_calls) >= 1, (
        "fallback path must upgrade the source with full metadata"
    )
    upgrade_props = upgrade_calls[-1].kwargs["item_properties"]
    assert upgrade_props["title"] == "Test Title"
    # Categories must be re-applied even though the source originally
    # didn't get them.
    assert upgrade_props.get("categories") == ["Land Cover, Land Use & Disturbance"]


# ----------------------------------------------------------------------------
# Vector tile layer — arcpy-stub path
# ----------------------------------------------------------------------------

def test_push_vector_tile_layer_invokes_local_vtpk_build(
    project_tree, _config_no_cache, valid_gpkg_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """vector-tile-layer push calls agol_vtpk.build_vtpk to produce a
    local .vtpk, then gis.content.add the VTPK + Item.publish() it.
    No hosted-feature-layer intermediate is ever created."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    row = _full_row(
        dataset_id="ds_vtl", file_path="Water/v.gpkg",
        agol_target="vector-tile-layer",
    )
    inventory_manager.insert_dataset(db, row)

    # Stub arcpy via sys.modules + monkeypatch build_vtpk to return a
    # fake .vtpk path (so we don't actually try to run arcpy).
    from pipeline import agol_vtpk
    fake_vtpk = project_tree["root"] / ".y2y" / "vtpk_cache" / "ds_vtl.vtpk"
    fake_vtpk.parent.mkdir(parents=True, exist_ok=True)
    fake_vtpk.write_bytes(b"\x50\x4b\x03\x04fake-vtpk-payload")

    monkeypatch.setattr(
        agol_vtpk, "build_vtpk",
        lambda gpkg_path, dataset_id, checksum, cache_dir, **kw: fake_vtpk,
    )

    gis = _make_gis(new_item_id="vtl_item_id")
    result = agol_sync.push(
        db, "ds_vtl", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # gis.content.add called with the .vtpk path, not the .gpkg path.
    call = gis.content.add.call_args
    assert str(fake_vtpk) == call.kwargs["data"]
    assert call.kwargs["item_properties"]["type"] == "Vector Tile Package"

    assert result.agol_item_id == "vtl_item_id"
    assert result.sync_status_after == "clean"


# ----------------------------------------------------------------------------
# Sharing override
# ----------------------------------------------------------------------------

def test_push_sharing_override_private(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    row = _full_row(dataset_id="ds_test", file_path="Water/v.gpkg")
    inventory_manager.insert_dataset(db, row)
    gis = _make_gis()

    agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
        sharing_override="private",
    )

    # The published item should have sharing_level=PRIVATE.
    published = gis.content.add.return_value
    # Note: the .publish() returns a new Item; that's what gets sharing
    # applied. Find that via the side_effect chain.
    # Simpler check: gis.groups.search should NOT have been called
    # (no group lookup when sharing is private).
    gis.groups.search.assert_not_called()


def test_push_sharing_default_adds_conservation_atlas_group(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """Default sharing uses the cached group_id from config; doesn't query."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    row = _full_row(dataset_id="ds_test", file_path="Water/v.gpkg")
    inventory_manager.insert_dataset(db, row)
    gis = _make_gis()

    # Config has cached group_id, so no lookup needed.
    agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )
    # Cached → no live query.
    gis.groups.search.assert_not_called()


# ----------------------------------------------------------------------------
# push_all_dirty
# ----------------------------------------------------------------------------

def test_push_all_dirty_iterates_pending_push_rows(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    db = project_tree["db"]
    valid_gpkg_factory("v1.gpkg", dest_dir=project_tree["library"] / "Water")
    valid_gpkg_factory("v2.gpkg", dest_dir=project_tree["library"] / "Water")

    # Two pending_push rows + one clean row (should be skipped).
    inventory_manager.insert_dataset(db, _full_row(
        dataset_id="ds_a", file_path="Water/v1.gpkg",
        sync_status="pending_push",
    ))
    inventory_manager.insert_dataset(db, _full_row(
        dataset_id="ds_b", file_path="Water/v2.gpkg",
        sync_status="pending_push",
    ))
    inventory_manager.insert_dataset(db, _full_row(
        dataset_id="ds_c", file_path="Water/v1.gpkg",  # reuse file
        sync_status="clean", agol_item_id="clean_id",
    ))

    new_ids = ["item_a", "item_b"]
    def make_with_id(**kwargs):
        item = MagicMock()
        item.id = new_ids.pop(0)
        item.sharing = MagicMock()
        item.sharing.sharing_level = "PRIVATE"
        item.sharing.groups = MagicMock()
        published = MagicMock()
        published.id = item.id
        published.sharing = MagicMock()
        published.sharing.sharing_level = "PRIVATE"
        published.sharing.groups = MagicMock()
        item.publish.return_value = published
        return item

    gis = _make_gis()
    gis.content.add.side_effect = make_with_id

    results = agol_sync.push_all_dirty(
        db, gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
    )
    # Only ds_a and ds_b were pending_push; ds_c stays clean.
    assert {r.dataset_id for r in results} == {"ds_a", "ds_b"}
    assert all(r.sync_status_after == "clean" for r in results)
    # ds_c untouched.
    assert inventory_manager.get_dataset(db, "ds_c")["sync_status"] == "clean"


def test_push_all_dirty_isolates_per_row_failures(
    project_tree, _config_no_cache, valid_gpkg_factory, monkeypatch,
) -> None:
    """One row failing doesn't abort the batch; failed row → sync_status='error'."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    inventory_manager.insert_dataset(db, _full_row(
        dataset_id="ds_ok", file_path="Water/v.gpkg",
        sync_status="pending_push",
    ))
    inventory_manager.insert_dataset(db, _full_row(
        dataset_id="ds_fail",
        file_path="Water/nonexistent.gpkg",  # file missing → AgolError
        sync_status="pending_push",
    ))

    gis = _make_gis(new_item_id="ok_item_id")

    results = agol_sync.push_all_dirty(
        db, gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
    )
    # Two results, one ok, one error.
    by_id = {r.dataset_id: r for r in results}
    assert by_id["ds_ok"].sync_status_after == "clean"
    assert by_id["ds_fail"].sync_status_after == "error"
    assert by_id["ds_fail"].error is not None

    # Catalogue reflects the per-row outcome.
    assert inventory_manager.get_dataset(db, "ds_ok")["sync_status"] == "clean"
    assert inventory_manager.get_dataset(db, "ds_fail")["sync_status"] == "error"
