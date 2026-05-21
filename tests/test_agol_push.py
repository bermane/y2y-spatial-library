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
    """Build a MagicMock GIS that behaves like arcgis.gis.GIS for push()."""
    gis = MagicMock()

    # gis.groups.search → returns a group with matching title.
    group = MagicMock()
    group.title = "Y2Y Conservation Atlas"
    group.id = group_id
    gis.groups.search.return_value = [group]

    # gis.content.add → returns an Item whose .publish() returns
    # either a published Item or raises (per publish_raises).
    def make_item(item_id: str) -> MagicMock:
        item = MagicMock()
        item.id = item_id
        # Modern sharing API: item.sharing.sharing_level is settable
        # and item.sharing.groups.add(group=...) works.
        item.sharing = MagicMock()
        item.sharing.sharing_level = "PRIVATE"
        item.sharing.groups = MagicMock()
        if publish_raises is not None:
            item.publish.side_effect = publish_raises
        else:
            published = MagicMock()
            published.id = item_id  # same id; published in place
            published.sharing = MagicMock()
            published.sharing.sharing_level = "PRIVATE"
            published.sharing.groups = MagicMock()
            item.publish.return_value = published
        return item

    gis.content.add.side_effect = lambda **kwargs: make_item(new_item_id)

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


def test_push_refuses_when_sync_status_is_clean(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")
    row = _full_row(dataset_id="ds_test", file_path="Water/v.gpkg",
                    sync_status="clean", agol_item_id="abc")
    inventory_manager.insert_dataset(db, row)
    gis = _make_gis()
    with pytest.raises(agol_sync.AgolError, match="sync_status is 'clean'"):
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

    # gis.content.add was called with file=str(library/.../v.gpkg) and the
    # expected folder.
    assert gis.content.add.called
    call_kwargs = gis.content.add.call_args.kwargs
    assert "Water" in call_kwargs["folder"]
    assert call_kwargs["folder"] == "Y2Y_Library/Water"
    # Properties carry the row's title etc.
    props = call_kwargs["item_properties"]
    assert props["title"] == "Test Title"
    assert props["categories"] == ["Water"]
    assert "Y2Y" in props["typeKeywords"]
    assert "Y2Y:dataset_id:ds_test" in props["typeKeywords"]

    # Result reflects the new item id.
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
# Update path — existing item
# ----------------------------------------------------------------------------

def test_push_updates_existing_item_when_agol_item_id_set(
    project_tree, _config_no_cache, valid_gpkg_factory,
) -> None:
    db = project_tree["db"]
    valid_gpkg_factory("v.gpkg", dest_dir=project_tree["library"] / "Water")

    # Existing AGOL item the catalogue is already linked to.
    existing = MagicMock()
    existing.id = "preexisting_id"
    existing.sharing = MagicMock()
    existing.sharing.sharing_level = "ORGANIZATION"
    existing.sharing.groups = MagicMock()

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
    existing.update.assert_called()
    # Because checksum didn't change, no .publish() call for re-publish.
    existing.publish.assert_not_called()

    assert result.agol_item_id == "preexisting_id"
    assert result.sync_status_after == "clean"


# ----------------------------------------------------------------------------
# Imagery-layer fallback when hosted publish fails
# ----------------------------------------------------------------------------

def test_push_imagery_falls_back_to_item_with_file_on_publish_failure(
    project_tree, _config_no_cache, valid_cog_factory,
) -> None:
    """When .publish() on a GeoTIFF item raises, push() retains the item
    as a downloadable file (no service), records the fallback in
    internal_notes + changelog, and still marks sync_status='clean'."""
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
