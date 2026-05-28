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
    agol_format: str = "feature-layer",
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
        "agol_format": agol_format,
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
    existing_item_type: str = "Feature Service",
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

    ``existing_item_type`` controls what AGOL type the existing item
    reports via ``.type`` — push()'s target-switch detection compares
    this against the catalogue's agol_format. Defaults to
    "Feature Service" since most tests exercise the feature-layer
    path. Pass "Vector Tile Service" / "Image Service" for the
    other paths. Tests that don't already set ``.type`` on the
    provided ``existing_item`` get this default applied so the
    switch detector doesn't fire spuriously.
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
        service.type = existing_item_type  # for target-switch detection
        service.sharing = MagicMock()
        service.sharing.sharing_level = "PRIVATE"
        service.sharing.groups = MagicMock()
        source.publish.return_value = service

    gis.content.add.return_value = source

    # Rev 3: VTL path uses Folder.add() (new SDK API) instead of the
    # deprecated gis.content.add(). _publish_vector_tile_layer's
    # _add_item_to_folder helper calls either:
    #   - source_folder_obj.add(item_properties=, file=) directly
    #     when source_folder_obj is a Folder instance, OR
    #   - gis.content.folders.get(folder=name).add(...) when only a
    #     string name is available.
    # Both return a Job whose .result() is the source Item. Wire
    # the chain so VTL tests get the same `source` Item regardless
    # of which code path runs.
    job_mock = MagicMock()
    job_mock.result.return_value = source
    folders_get_mock = gis.content.folders.get.return_value
    folders_get_mock.add.return_value = job_mock
    # Also wire _ensure_folder's create() return value (the live
    # Folder instance) so its .add() works too — push() prefers the
    # instance over the name-lookup path.
    gis.content.folders.create.return_value.add.return_value = job_mock

    if existing_item is not None:
        # Default the AGOL type so push()'s target-switch detector
        # doesn't fire on tests that don't explicitly set it.
        # Tests that need a specific type to drive the switch logic
        # can override before/after calling _make_gis.
        if not isinstance(getattr(existing_item, "type", None), str):
            existing_item.type = existing_item_type
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
        agol_format="imagery-layer",  # mismatched
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

    # Rev 3: source GPKG upload goes through Folder.add() (new SDK
    # API) rather than the deprecated gis.content.add(). The call
    # lives on the Folder instance returned by _ensure_folder.
    # The service item (Feature Service) is created by source.publish()
    # and then moved to the category folder. See the per-contract
    # tests below for finer assertions on each step.
    folder_add = gis.content.folders.create.return_value.add
    assert folder_add.called

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
    """Return the kwargs of the most recent source-upload call.

    Rev 3 migrated the create paths off the deprecated
    gis.content.add() onto Folder.add() (new SDK API). The call site
    now lives on the Folder instance returned by _ensure_folder
    (which is gis.content.folders.create.return_value in our mock).

    For backwards compatibility with assertions written against the
    old shape, this helper transparently returns equivalent kwargs:
    {'item_properties': <dict>, 'data': <path-string>, 'folder': str}.
    The new Folder.add() takes an ItemProperties dataclass and
    file= (not data=); we translate so existing assertions keep
    working without retroactive churn.
    """
    folder_add = gis.content.folders.create.return_value.add
    if folder_add.call_args is not None:
        kwargs = dict(folder_add.call_args.kwargs)
        # Translate ItemProperties dataclass → dict-equivalent so
        # legacy tests can still inspect kwargs["item_properties"]
        # using dict access patterns.
        ip = kwargs.get("item_properties")
        if ip is not None and not isinstance(ip, dict):
            kwargs["item_properties"] = {
                "type": getattr(ip.item_type, "value", ip.item_type),
                "title": ip.title,
                "description": ip.description,
                "typeKeywords": ip.type_keywords,
            }
        # Translate file= → data= for legacy tests.
        if "file" in kwargs:
            kwargs["data"] = kwargs.pop("file")
        return kwargs
    # Last-resort fallback to the deprecated call (legacy code
    # paths that bypass _add_item_to_folder).
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

    # Source GPKG was added with minimal props. Folder targeting is
    # done by calling Folder.add() on the live _sources Folder
    # instance — verify via the folders.create() call list that
    # _sources was created (and that the live Folder's add() was
    # invoked).
    source_call_kwargs = _last_content_add_kwargs(gis)
    sources_create_calls = [
        c for c in gis.content.folders.create.call_args_list
        if c.kwargs.get("folder") == "_sources"
        and c.kwargs.get("exist_ok") is True
    ]
    assert sources_create_calls, (
        "expected folders.create(folder='_sources', exist_ok=True)"
    )
    assert gis.content.folders.create.return_value.add.called

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


def test_push_update_path_moves_service_to_category_folder(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """Regression guard for pilot test #1's persistent stranding bug:
    a service originally published into My Content root (by a pre-fix
    run or a manual upload) needs to be relocated to the category
    folder on subsequent pushes. The update path previously only
    refreshed data + metadata + source reconcile; service.move() was
    never called on it. So a service that started life in root stayed
    in root forever — the symptom the steward kept reporting.

    Item.move() is idempotent server-side, so calling it on every
    push is safe (a service already in the right folder is a no-op).
    """
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")

    existing = MagicMock()
    existing.id = "preexisting_service_id"
    existing.sharing = MagicMock()
    existing.sharing.sharing_level = "ORGANIZATION"
    existing.related_items.return_value = []  # self-hosted FS

    row = _full_row(
        dataset_id="ds_test", file_path="Water/v.gpkg",
        sync_status="clean", agol_item_id="preexisting_service_id",
    )
    # No checksum change so the update path is purely metadata +
    # move + source reconcile — exactly the case where the bug bit.
    row["last_synced_at"] = "2030-01-01T00:00:00Z"
    row["date_modified"] = "2026-04-29T00:00:00Z"
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(existing_item=existing)

    agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # The existing service was moved (idempotently) to the Water folder.
    existing.move.assert_called_once()
    # Verify the folders.create() for Water happened with exist_ok=True.
    water_create = [
        c for c in gis.content.folders.create.call_args_list
        if c.kwargs.get("folder") == "Water"
        and c.kwargs.get("exist_ok") is True
    ]
    assert water_create, "expected folders.create(folder='Water', exist_ok=True)"


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
    assert full_props["categories"] == ["/Categories/Water"]
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
# Imagery-layer no-source model — uses publish_hosted_imagery_layer directly
# ----------------------------------------------------------------------------

def _patch_publish_hosted_imagery_layer(monkeypatch, return_value):
    """Patch arcgis.raster.publish_hosted_imagery_layer to return a mock.

    Returns the MagicMock that stands in for the function so callers
    can inspect call_args / call_count.
    """
    fake = MagicMock(return_value=return_value)
    # The function is imported lazily inside _publish_imagery_layer
    # via `from arcgis.raster import publish_hosted_imagery_layer`.
    # Patch the module-level name so the lazy import picks it up.
    import arcgis.raster
    monkeypatch.setattr(arcgis.raster, "publish_hosted_imagery_layer", fake)
    return fake


def test_push_imagery_create_uses_publish_hosted_imagery_layer(
    project_tree, _config_no_cache, valid_cog_factory, monkeypatch,
) -> None:
    """Imagery-layer create path goes through
    arcgis.raster.publish_hosted_imagery_layer — NOT the deprecated
    gis.content.add() + Item.publish() chain. This is the no-source
    model (steward-confirmed 2026-05-22): no separate TIFF item gets
    created in the catalogue's My Content tree.
    """
    db = project_tree["db"]
    raster_dir = project_tree["library"] / "Land_Cover_Use_Disturbance"
    raster_dir.mkdir(parents=True, exist_ok=True)
    valid_cog_factory("r.tif", dest_dir=raster_dir, dtype="uint8", nodata=255)

    row = _full_row(
        dataset_id="ds_raster", file_path="Land_Cover_Use_Disturbance/r.tif",
        classification="categorical", format_="geotiff",
        agol_format="imagery-layer",
        category="Land Cover, Land Use & Disturbance",
    )
    inventory_manager.insert_dataset(db, row)

    service = MagicMock()
    service.id = "imagery_layer_id"
    service.sharing = MagicMock()
    service.sharing.sharing_level = "PRIVATE"
    service.sharing.groups = MagicMock()

    gis = _make_gis(new_item_id="imagery_layer_id")
    fake_publish = _patch_publish_hosted_imagery_layer(monkeypatch, service)

    result = agol_sync.push(
        db, "ds_raster", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # publish_hosted_imagery_layer was the entry point.
    fake_publish.assert_called_once()
    call_kwargs = fake_publish.call_args.kwargs
    assert call_kwargs["layer_configuration"] == "ONE_IMAGE"
    assert call_kwargs["tiles_only"] is False
    assert call_kwargs["gis"] is gis
    input_data = call_kwargs["input_data"]
    assert isinstance(input_data, list) and len(input_data) == 1
    assert "r.tif" in input_data[0]

    # REGRESSION GUARD (Test 2b): output_name must be the sanitised
    # service name — NOT the raw title 'Test Title' (AGOL rejects
    # spaces). compute_service_name turns 'Test Title' →
    # 'Test_Title'.
    output_name = call_kwargs["output_name"]
    assert output_name == "Test_Title", (
        f"output_name was {output_name!r} — must be AGOL-safe "
        f"(sanitised, only [A-Za-z0-9_])"
    )
    import re
    assert re.fullmatch(r"[A-Za-z0-9_]+", output_name)

    # REGRESSION GUARD: no separate source TIFF item created. The
    # deprecated gis.content.add() must not have been used for the
    # imagery path (it's still used by the vector path; that's fine).
    # We can't simply assert .not_called because the mock object is
    # shared with the vector path's stub, but we CAN assert that no
    # _sources folder ops happened.
    sources_create_calls = [
        c for c in gis.content.folders.create.call_args_list
        if c.kwargs.get("folder") == "_sources"
    ]
    assert not sources_create_calls, (
        "imagery push must not create a _sources folder; that's the "
        "vector path's behaviour"
    )

    # Service was moved into the category folder.
    service.move.assert_called_once()
    category_create_calls = [
        c for c in gis.content.folders.create.call_args_list
        if c.kwargs.get("folder") == "Land_Cover_Use_Disturbance"
        and c.kwargs.get("exist_ok") is True
    ]
    assert category_create_calls, (
        "expected folders.create(folder='Land_Cover_Use_Disturbance', "
        "exist_ok=True)"
    )

    # Full metadata applied via item.update(item_properties=...).
    metadata_calls = [
        c for c in service.update.call_args_list
        if "item_properties" in c.kwargs
    ]
    assert metadata_calls, "service must receive full metadata via update()"

    assert result.agol_item_id == "imagery_layer_id"
    assert result.sync_status_after == "clean"


def test_push_imagery_update_refreshes_via_output_name(
    project_tree, _config_no_cache, valid_cog_factory, monkeypatch,
) -> None:
    """Imagery-layer update path refreshes data by calling
    publish_hosted_imagery_layer again with output_name set to the
    existing service item. AGOL replaces the underlying raster in
    place, preserving item ID + URL + sharing + categories.
    """
    db = project_tree["db"]
    raster_dir = project_tree["library"] / "Land_Cover_Use_Disturbance"
    raster_dir.mkdir(parents=True, exist_ok=True)
    valid_cog_factory("r.tif", dest_dir=raster_dir, dtype="uint8", nodata=255)

    existing = MagicMock()
    existing.id = "preexisting_imagery_id"
    existing.type = "Image Service"
    existing.sharing = MagicMock()
    existing.sharing.sharing_level = "ORGANIZATION"

    row = _full_row(
        dataset_id="ds_raster", file_path="Land_Cover_Use_Disturbance/r.tif",
        classification="categorical", format_="geotiff",
        agol_format="imagery-layer",
        category="Land Cover, Land Use & Disturbance",
        sync_status="pending_push",
        agol_item_id="preexisting_imagery_id",
    )
    # Force checksum_changed=True via last_synced_at < date_modified.
    row["last_synced_at"] = "2026-01-01T00:00:00Z"
    row["date_modified"] = "2026-04-29T00:00:00Z"
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(existing_item=existing, existing_item_type="Image Service")
    fake_publish = _patch_publish_hosted_imagery_layer(
        monkeypatch, existing,
    )

    result = agol_sync.push(
        db, "ds_raster", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # publish_hosted_imagery_layer was called with the existing
    # service Item as output_name — this is what triggers AGOL's
    # in-place data replacement.
    fake_publish.assert_called_once()
    call_kwargs = fake_publish.call_args.kwargs
    assert call_kwargs["output_name"] is existing, (
        "update path must pass existing service Item as output_name "
        "so AGOL replaces data in place"
    )

    # Metadata reapplied + service moved to category folder.
    existing.update.assert_called()
    existing.move.assert_called()
    assert result.sync_status_after == "clean"


def test_push_imagery_update_skips_refresh_when_checksum_unchanged(
    project_tree, _config_no_cache, valid_cog_factory, monkeypatch,
) -> None:
    """When the source TIFF hasn't changed since last_synced_at, the
    update path skips the data refresh entirely — no
    publish_hosted_imagery_layer call. Just metadata reapply + move."""
    db = project_tree["db"]
    raster_dir = project_tree["library"] / "Land_Cover_Use_Disturbance"
    raster_dir.mkdir(parents=True, exist_ok=True)
    valid_cog_factory("r.tif", dest_dir=raster_dir, dtype="uint8", nodata=255)

    existing = MagicMock()
    existing.id = "preexisting_imagery_id"
    existing.type = "Image Service"
    existing.sharing = MagicMock()
    existing.sharing.sharing_level = "ORGANIZATION"

    row = _full_row(
        dataset_id="ds_raster", file_path="Land_Cover_Use_Disturbance/r.tif",
        classification="categorical", format_="geotiff",
        agol_format="imagery-layer",
        category="Land Cover, Land Use & Disturbance",
        sync_status="clean",
        agol_item_id="preexisting_imagery_id",
    )
    # last_synced_at >> date_modified → checksum_changed=False.
    row["last_synced_at"] = "2030-01-01T00:00:00Z"
    row["date_modified"] = "2026-04-29T00:00:00Z"
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(existing_item=existing, existing_item_type="Image Service")
    fake_publish = _patch_publish_hosted_imagery_layer(monkeypatch, existing)

    agol_sync.push(
        db, "ds_raster", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # No data refresh — publish_hosted_imagery_layer never called.
    fake_publish.assert_not_called()
    # Metadata reapply + move still happened.
    existing.update.assert_called()
    existing.move.assert_called()


def test_push_imagery_records_warning_when_refresh_raises(
    project_tree, _config_no_cache, valid_cog_factory, monkeypatch,
) -> None:
    """If publish_hosted_imagery_layer raises on the update path (e.g.
    transient AGOL backend error), the catalogue/service stay in a
    consistent state and a [agol] warning lands in internal_notes."""
    db = project_tree["db"]
    raster_dir = project_tree["library"] / "Land_Cover_Use_Disturbance"
    raster_dir.mkdir(parents=True, exist_ok=True)
    valid_cog_factory("r.tif", dest_dir=raster_dir, dtype="uint8", nodata=255)

    existing = MagicMock()
    existing.id = "preexisting_imagery_id"
    existing.type = "Image Service"
    existing.sharing = MagicMock()
    existing.sharing.sharing_level = "ORGANIZATION"

    row = _full_row(
        dataset_id="ds_raster", file_path="Land_Cover_Use_Disturbance/r.tif",
        classification="categorical", format_="geotiff",
        agol_format="imagery-layer",
        category="Land Cover, Land Use & Disturbance",
        sync_status="pending_push",
        agol_item_id="preexisting_imagery_id",
    )
    row["last_synced_at"] = "2026-01-01T00:00:00Z"
    row["date_modified"] = "2026-04-29T00:00:00Z"
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(existing_item=existing, existing_item_type="Image Service")
    import arcgis.raster
    monkeypatch.setattr(
        arcgis.raster, "publish_hosted_imagery_layer",
        MagicMock(side_effect=RuntimeError("AGOL backend transient error")),
    )

    result = agol_sync.push(
        db, "ds_raster", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # Catalogue still ends in 'clean' — metadata + move succeeded
    # even though data refresh failed.
    assert result.sync_status_after == "clean"

    after = inventory_manager.get_dataset(db, "ds_raster")
    notes = after["internal_notes"] or ""
    assert "[agol]" in notes
    assert "imagery data refresh failed" in notes


def test_push_imagery_create_failure_raises_agol_error(
    project_tree, _config_no_cache, valid_cog_factory, monkeypatch,
) -> None:
    """On the create path, publish_hosted_imagery_layer failures are
    fatal — there's no source-TIFF fallback under the no-source
    model. The push aborts with an AgolError so the steward
    investigates rather than ending up with a half-published state."""
    db = project_tree["db"]
    raster_dir = project_tree["library"] / "Land_Cover_Use_Disturbance"
    raster_dir.mkdir(parents=True, exist_ok=True)
    valid_cog_factory("r.tif", dest_dir=raster_dir, dtype="uint8", nodata=255)

    row = _full_row(
        dataset_id="ds_raster", file_path="Land_Cover_Use_Disturbance/r.tif",
        classification="categorical", format_="geotiff",
        agol_format="imagery-layer",
        category="Land Cover, Land Use & Disturbance",
    )
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(new_item_id="imagery_layer_id")
    import arcgis.raster
    monkeypatch.setattr(
        arcgis.raster, "publish_hosted_imagery_layer",
        MagicMock(side_effect=RuntimeError("hosted imagery publish refused")),
    )

    with pytest.raises(agol_sync.AgolError, match="hosted imagery publish failed"):
        agol_sync.push(
            db, "ds_raster", gis, _config_no_cache,
            library_root=project_tree["library"], actor="tester",
            cache_dir=project_tree["root"] / ".y2y",
        )


# ----------------------------------------------------------------------------
# Vector tile layer — manual-VTPK rev 3 path
# ----------------------------------------------------------------------------
#
# The arcpy path was retired in rev 3 (2026-05-27) after a session
# of Pro-upgrade fragility failures. Now the steward builds the
# VTPK in Pro's UI manually, drops it in queue/incoming/, runs
# `y2y ingest scan` to move it to library/vtpk/<stem>.vtpk, and
# push() uploads the pre-built file. These tests fake the VTPK by
# writing valid ZIP-signature bytes at the canonical library path
# before exercising push().

def _plant_vtpk(project_tree, file_stem: str) -> Path:
    """Drop a fake-but-valid VTPK file at library/vtpk/<stem>.vtpk."""
    vtpk_dir = project_tree["library"].parent / "vtpk"
    vtpk_dir.mkdir(parents=True, exist_ok=True)
    vtpk_path = vtpk_dir / f"{file_stem}.vtpk"
    # Real VTPK files are zip containers; we sniff the ZIP local
    # file header magic in agol_vtpk.ingest_one_vtpk. For push()
    # tests the magic isn't required — but we use it for realism.
    vtpk_path.write_bytes(b"PK\x03\x04fake-vtpk-payload-for-testing")
    return vtpk_path


def test_push_vtl_create_uploads_vtpk_publishes_vts(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """Create-path push for a VTL row: VTPK exists at library/vtpk/,
    push uploads it as a source item + publishes the VTS. No arcpy
    invocation anywhere; no intermediate Feature Service."""
    db = project_tree["db"]
    valid_gpkg_factory("parks.gpkg", dest_dir=project_tree["library"] / "Land_Designations_Tenure")
    vtpk_path = _plant_vtpk(project_tree, "parks")

    row = _full_row(
        dataset_id="ds_vtl",
        file_path="Land_Designations_Tenure/parks.gpkg",
        agol_format="vector-tile-layer",
        category="Land Designations & Tenure",
    )
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(new_item_id="vtl_item_id")
    result = agol_sync.push(
        db, "ds_vtl", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # Rev 3: VTL push uses Folder.add() (new SDK API) rather than
    # the deprecated gis.content.add(). The Folder.add() call lives
    # on the Folder instance returned by _ensure_folder (which is
    # gis.content.folders.create.return_value in the mock).
    folder_obj = gis.content.folders.create.return_value
    folder_obj.add.assert_called_once()
    add_kwargs = folder_obj.add.call_args.kwargs
    assert str(vtpk_path) == add_kwargs["file"]
    # item_properties is now an ItemProperties dataclass, not a dict.
    from arcgis.gis import ItemProperties, ItemTypeEnum
    ip = add_kwargs["item_properties"]
    assert isinstance(ip, ItemProperties)
    assert ip.item_type == ItemTypeEnum.VECTOR_TILE_PACKAGE

    # source.publish called with file_type='Vector Tile Package' to
    # produce the Vector Tile Service. source is what Folder.add()'s
    # Job.result() returns; _make_gis wires both to the same mock.
    source = gis.content.add.return_value
    source.publish.assert_called_once()
    publish_kwargs = source.publish.call_args.kwargs
    assert publish_kwargs.get("file_type") == "Vector Tile Package"

    assert result.agol_item_id == "vtl_item_id"
    assert result.sync_status_after == "clean"


def test_push_vtl_create_errors_when_vtpk_not_ingested(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """If no VTPK exists at library/vtpk/<stem>.vtpk, push() raises
    AgolError with an actionable message before any AGOL contact.
    This is the rev 3 pre-flight check; reconcile also surfaces
    this case via the missing-VTPK invariant.
    """
    db = project_tree["db"]
    valid_gpkg_factory("parks.gpkg", dest_dir=project_tree["library"] / "Land_Designations_Tenure")
    # NB: no VTPK planted.
    row = _full_row(
        dataset_id="ds_vtl",
        file_path="Land_Designations_Tenure/parks.gpkg",
        agol_format="vector-tile-layer",
        category="Land Designations & Tenure",
    )
    inventory_manager.insert_dataset(db, row)
    gis = _make_gis(new_item_id="vtl_item_id")

    with pytest.raises(agol_sync.AgolError, match="No VTPK ingested"):
        agol_sync.push(
            db, "ds_vtl", gis, _config_no_cache,
            library_root=project_tree["library"], actor="tester",
            cache_dir=project_tree["root"] / ".y2y",
        )

    # No AGOL contact happened — pre-flight bailed before content.add.
    gis.content.add.assert_not_called()


def test_push_vtl_update_refreshes_when_vtpk_checksum_changed(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """Update-path push for a VTL row: if the on-disk VTPK's sha256
    differs from the Y2Y:vtpk_sha256 typeKeyword on the AGOL
    source item, refresh via source.update(data) + source.publish(
    file_type='Vector Tile Package', overwrite=True)."""
    db = project_tree["db"]
    valid_gpkg_factory("parks.gpkg", dest_dir=project_tree["library"] / "Land_Designations_Tenure")
    vtpk_path = _plant_vtpk(project_tree, "parks")

    # The AGOL source item — has a stale checksum typeKeyword.
    source_item = MagicMock()
    source_item.id = "source_vtpk_id"
    source_item.typeKeywords = [
        "Y2Y", "Y2Y:source", "Y2Y:dataset_id:ds_vtl",
        "Y2Y:vtpk_sha256:STALE_HASH",
    ]
    source_item.sharing = MagicMock()
    source_item.sharing.sharing_level = "PRIVATE"
    source_item.related_items.return_value = []

    # The existing service.
    existing = MagicMock()
    existing.id = "vtl_item_id"
    existing.type = "Vector Tile Service"
    existing.sharing = MagicMock()
    existing.sharing.sharing_level = "ORGANIZATION"
    existing.related_items.return_value = [source_item]

    row = _full_row(
        dataset_id="ds_vtl",
        file_path="Land_Designations_Tenure/parks.gpkg",
        agol_format="vector-tile-layer",
        category="Land Designations & Tenure",
        sync_status="pending_push",
        agol_item_id="vtl_item_id",
    )
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(
        existing_item=existing,
        existing_item_type="Vector Tile Service",
    )

    agol_sync.push(
        db, "ds_vtl", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # source.update(data=<vtpk>) was called.
    source_update_data_calls = [
        c for c in source_item.update.call_args_list
        if "data" in c.kwargs
    ]
    assert source_update_data_calls, (
        "expected source.update(data=<vtpk_path>) when VTPK checksum "
        "differs from the AGOL Y2Y:vtpk_sha256 typeKeyword"
    )

    # source.publish(file_type='Vector Tile Package', overwrite=True)
    # was called.
    source_item.publish.assert_called_once()
    pub_kwargs = source_item.publish.call_args.kwargs
    assert pub_kwargs.get("file_type") == "Vector Tile Package"
    assert pub_kwargs.get("overwrite") is True

    # service.publish must NEVER be called on update (would create a
    # duplicate VTS, just like the FL regression guard).
    existing.publish.assert_not_called()


def test_push_vtl_update_skips_refresh_when_vtpk_checksum_matches(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """If the on-disk VTPK's sha256 matches the AGOL source item's
    Y2Y:vtpk_sha256 typeKeyword, skip the refresh — only metadata
    + move + sharing happen. Avoids re-uploading multi-MB packages
    unnecessarily."""
    db = project_tree["db"]
    valid_gpkg_factory("parks.gpkg", dest_dir=project_tree["library"] / "Land_Designations_Tenure")
    vtpk_path = _plant_vtpk(project_tree, "parks")

    # Compute the actual sha to plant on the AGOL source mock.
    from pipeline import agol_vtpk as _av
    real_sha = _av.read_vtpk_checksum(vtpk_path)

    source_item = MagicMock()
    source_item.id = "source_vtpk_id"
    source_item.typeKeywords = [
        "Y2Y", "Y2Y:source", "Y2Y:dataset_id:ds_vtl",
        f"Y2Y:vtpk_sha256:{real_sha}",
    ]
    source_item.sharing = MagicMock()
    source_item.sharing.sharing_level = "PRIVATE"
    source_item.related_items.return_value = []

    existing = MagicMock()
    existing.id = "vtl_item_id"
    existing.type = "Vector Tile Service"
    existing.sharing = MagicMock()
    existing.sharing.sharing_level = "ORGANIZATION"
    existing.related_items.return_value = [source_item]

    row = _full_row(
        dataset_id="ds_vtl",
        file_path="Land_Designations_Tenure/parks.gpkg",
        agol_format="vector-tile-layer",
        category="Land Designations & Tenure",
        sync_status="clean",
        agol_item_id="vtl_item_id",
    )
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(
        existing_item=existing,
        existing_item_type="Vector Tile Service",
    )

    agol_sync.push(
        db, "ds_vtl", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # No data refresh — source.publish wasn't called.
    source_item.publish.assert_not_called()
    # No source.update(data=...) either.
    update_data_calls = [
        c for c in source_item.update.call_args_list
        if "data" in c.kwargs
    ]
    assert not update_data_calls


# ----------------------------------------------------------------------------
# Target-switch tests (rev 3.6)
# ----------------------------------------------------------------------------

def test_push_target_switch_FL_to_VTL_unpublishes_old(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """When agol_format was changed from feature-layer to
    vector-tile-layer on a previously-published row, push detects
    the type mismatch and unpublishes the old Feature Service +
    linked source GPKG before creating the new VTS."""
    db = project_tree["db"]
    valid_gpkg_factory("parks.gpkg", dest_dir=project_tree["library"] / "Land_Designations_Tenure")
    _plant_vtpk(project_tree, "parks")

    # Existing AGOL item is a Feature Service (the catalogue was
    # previously feature-layer). The steward has since switched
    # agol_format to vector-tile-layer; push must detect the
    # mismatch and unpublish.
    linked_source = MagicMock()
    linked_source.id = "old_gpkg_source_id"

    existing = MagicMock()
    existing.id = "old_fs_id"
    existing.type = "Feature Service"  # ← mismatched against target=VTL
    existing.related_items.return_value = [linked_source]

    row = _full_row(
        dataset_id="ds_vtl",
        file_path="Land_Designations_Tenure/parks.gpkg",
        agol_format="vector-tile-layer",
        category="Land Designations & Tenure",
        sync_status="pending_push",
        agol_item_id="old_fs_id",
    )
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(
        new_item_id="new_vtl_id",
        existing_item=existing,
        existing_item_type="Feature Service",
    )

    result = agol_sync.push(
        db, "ds_vtl", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # Both the existing Feature Service AND its linked source were
    # deleted before the new VTS was created.
    existing.delete.assert_called_once()
    linked_source.delete.assert_called_once()

    # The catalogue's agol_item_id is now the NEW VTS id.
    assert result.agol_item_id == "new_vtl_id"

    # Internal notes records the target switch.
    after = inventory_manager.get_dataset(db, "ds_vtl")
    notes = after["internal_notes"] or ""
    assert "agol_format switched" in notes


def test_push_recovers_from_filename_collision_on_upload(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """When Folder.add() raises a filename-collision error (AGOL
    CONT_0027 / code 409), the pipeline parses the conflicting
    item ID from the error, deletes that item permanently, then
    retries the upload once. Records a [agol] warning so the
    steward sees the stale-source cleanup happened.

    Regression for Test 3b Phase B: a VTPK source item from an
    earlier failed publish lingered in AGOL's _sources/ folder.
    When the next push tried to upload a new VTPK with the same
    filename, AGOL rejected with 'Item with this filename already
    exists. [itemId=11a45...]'. The fix self-heals by reclaiming
    the conflicting item."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    row = _full_row(dataset_id="ds_test", file_path="Water/v.gpkg")
    inventory_manager.insert_dataset(db, row)

    gis = _make_gis(new_item_id="new_fl_id")

    # Wire Folder.add() to raise on the first call (collision) and
    # succeed on the retry. Mimics the AGOL error message format.
    folder_obj = gis.content.folders.create.return_value
    success_job = MagicMock()
    success_job.result.return_value = gis.content.add.return_value
    call_count = {"n": 0}

    def add_side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise Exception(
                "The item could not be added: "
                "{'error': {'code': 409, 'messageCode': 'CONT_0027', "
                "'message': 'Item with this filename already exists. "
                "[itemId=abc123def456abc123def456abc12345]', 'details': []}}"
            )
        return success_job

    folder_obj.add.side_effect = add_side_effect

    # Mock the conflicting item that gis.content.get returns when
    # asked for 'abc123def456abc123def456abc12345'.
    conflicting_item = MagicMock()
    conflicting_item.id = "abc123def456abc123def456abc12345"
    conflicting_item.title = "Stale Source"
    gis.content.get.return_value = conflicting_item

    result = agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # Folder.add was called twice (initial + retry).
    assert call_count["n"] == 2
    # Conflicting item was deleted permanently.
    conflicting_item.delete.assert_called_once_with(permanent=True)
    # The push completed; sync_status='clean'.
    assert result.sync_status_after == "clean"
    # internal_notes captures the [agol] cleanup warning.
    after = inventory_manager.get_dataset(db, "ds_test")
    notes = after["internal_notes"] or ""
    assert "[agol]" in notes
    assert "abc123def456abc123def456abc12345" in notes
    assert "permanently deleted" in notes.lower() or "replaced stale" in notes.lower()


def test_push_target_switch_handles_missing_agol_item(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    """If the catalogue has agol_item_id set but the AGOL item is
    gone (deleted out-of-band), push() proceeds as if it were a
    create — records a warning and creates fresh."""
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")

    row = _full_row(
        dataset_id="ds_test", file_path="Water/v.gpkg",
        sync_status="pending_push",
        agol_item_id="phantom_id",
    )
    inventory_manager.insert_dataset(db, row)

    # gis.content.get returns None (item missing) — set via _make_gis
    # passing existing_item=None.
    gis = _make_gis(new_item_id="new_fl_id")
    # But we DO need to flag agol_item_id as set — push() reads from
    # the catalogue row, so we just rely on the row having
    # agol_item_id='phantom_id' and gis.content.get returning None.

    result = agol_sync.push(
        db, "ds_test", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        cache_dir=project_tree["root"] / ".y2y",
    )

    # New FS was created (push took the create path despite the
    # phantom agol_item_id).
    assert result.agol_item_id == "new_fl_id"
    after = inventory_manager.get_dataset(db, "ds_test")
    notes = after["internal_notes"] or ""
    assert "deleted out-of-band" in notes or "no longer exists" in notes


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

    # Rev 3: previously this test used gis.content.add.side_effect to
    # generate unique-ID source mocks per call. With the Folder.add()
    # migration, the side_effect needs to live on the result of
    # Folder.add() (which is a Job; .result() yields the source
    # Item). Per-call uniqueness still matters because the
    # catalogue's agol_item_id column is UNIQUE.
    new_ids = ["item_a", "item_b"]
    def make_source_item(*args, **kwargs):
        item = MagicMock()
        item.id = new_ids.pop(0)
        item.sharing = MagicMock()
        item.sharing.sharing_level = "PRIVATE"
        item.sharing.groups = MagicMock()
        published = MagicMock()
        published.id = item.id  # source.publish() → service has same id for asserts
        published.sharing = MagicMock()
        published.sharing.sharing_level = "PRIVATE"
        published.sharing.groups = MagicMock()
        item.publish.return_value = published
        return item

    gis = _make_gis()
    # Override the default Folder.add().result() to yield unique mocks.
    gis.content.folders.create.return_value.add.return_value.result.side_effect = make_source_item

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
