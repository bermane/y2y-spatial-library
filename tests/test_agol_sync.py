"""Unit tests for the AGOL integration skeleton.

These tests never reach the network; the ``arcgis.gis.GIS`` class is
replaced with a stub for every test that exercises a code path that
would otherwise contact AGOL. Pure-function tests
(``compute_target_folder``, ``compute_agol_category``,
``compute_item_properties``) don't need a stub at all.

Phase A scope: configuration loading, mapping helpers, exception
paths, the category-schema merge helper. Phase B will extend this
file with push() coverage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from pipeline import agol_config, agol_sync


# -----------------------------------------------------------------------------
# compute_target_folder — catalogue category → AGOL folder
# -----------------------------------------------------------------------------

def test_compute_target_folder_mirrors_catalogue_folder() -> None:
    """AGOL folders are flat (no nesting / no slashes). The function
    returns just the underscored category folder name — matches what
    the steward sees in ``library/spatial/``."""
    assert agol_sync.compute_target_folder("Species") == "Species"
    assert (
        agol_sync.compute_target_folder("Jurisdictional & Political Boundaries")
        == "Juris_Political_Boundaries"
    )
    assert (
        agol_sync.compute_target_folder("Land Designations & Tenure")
        == "Land_Designations_Tenure"
    )


def test_compute_target_folder_returns_bare_name_with_no_slash() -> None:
    """Regression guard: a previous implementation prepended a
    ``Y2Y_Library/`` namespace prefix. AGOL accepted the slash as a
    literal character in ``content.add(folder=...)`` (creating a folder
    literally named with the slash) but ``Item.move(folder=...)``
    silently failed against the same string, stranding feature layers
    in My Content root. Stripping the prefix sidesteps both bugs."""
    for cat in (
        "Species", "Water", "Jurisdictional & Political Boundaries",
        "Land Cover, Land Use & Disturbance",
    ):
        out = agol_sync.compute_target_folder(cat)
        assert "/" not in out, (
            f"compute_target_folder({cat!r}) returned {out!r} — folder "
            f"names must not contain slashes."
        )


def test_compute_target_folder_rejects_unknown_category() -> None:
    with pytest.raises(agol_sync.AgolError, match="not one of"):
        agol_sync.compute_target_folder("Not A Real Category")


# -----------------------------------------------------------------------------
# compute_agol_category — identity for valid display names
# -----------------------------------------------------------------------------

def test_compute_agol_category_is_identity_for_typology_categories() -> None:
    """The AGOL Content Category equals the catalogue display name verbatim,
    never the underscored folder name."""
    assert (
        agol_sync.compute_agol_category("Jurisdictional & Political Boundaries")
        == "Jurisdictional & Political Boundaries"
    )
    assert agol_sync.compute_agol_category("Species") == "Species"
    assert agol_sync.compute_agol_category("Human Dimensions") == "Human Dimensions"


def test_compute_agol_category_rejects_unknown() -> None:
    with pytest.raises(agol_sync.AgolError, match="not one of"):
        agol_sync.compute_agol_category("Made Up")


# -----------------------------------------------------------------------------
# compute_service_name — title → AGOL-safe service name
# -----------------------------------------------------------------------------

def test_compute_service_name_strips_spaces_and_special_chars() -> None:
    """AGOL service names allow only [A-Za-z0-9_]. Anything else
    (spaces, parens, dashes, slashes, em-dashes, etc.) must be
    sanitised. Regression guard for Test 2b's
    'service name cannot contain spaces or special characters'
    error against the title 'Y2Y Land Cover (2020)'."""
    cases = {
        "Y2Y Land Cover (2020)": "Y2Y_Land_Cover_2020",
        "Biomass Carbon Density 2022 (t/ha)": "Biomass_Carbon_Density_2022_t_ha",
        "GB Habitat — Female Fall": "GB_Habitat_Female_Fall",
        "  leading/trailing  ": "leading_trailing",
        "Multiple   spaces": "Multiple_spaces",
        "Already_safe_name": "Already_safe_name",
        "All*special#chars!": "All_special_chars",
    }
    for title, expected in cases.items():
        row = {"title": title, "dataset_id": "ds_fallback"}
        assert agol_sync.compute_service_name(row) == expected, (
            f"compute_service_name({title!r}) → "
            f"{agol_sync.compute_service_name(row)!r}, expected {expected!r}"
        )


def test_compute_service_name_only_contains_safe_chars() -> None:
    """Property check: output is always [A-Za-z0-9_]+ regardless of
    input. AGOL rejects anything else."""
    import re
    for title in (
        "Y2Y Land Cover (2020)",
        "100% pure — exotic /\\ chars!",
        "üñîçødé",
        "  ",  # would fall back to dataset_id
    ):
        out = agol_sync.compute_service_name(
            {"title": title, "dataset_id": "ds_01ABC"}
        )
        assert re.fullmatch(r"[A-Za-z0-9_]+", out), (
            f"compute_service_name({title!r}) returned {out!r} which "
            f"contains AGOL-illegal characters"
        )


def test_compute_service_name_falls_back_to_dataset_id_on_empty() -> None:
    """When the title is empty or sanitises to nothing, fall back to
    the dataset_id (ULID format is AGOL-safe by construction)."""
    assert agol_sync.compute_service_name(
        {"title": "", "dataset_id": "ds_01HJK"}
    ) == "ds_01HJK"
    assert agol_sync.compute_service_name(
        {"title": "!!!", "dataset_id": "ds_01XYZ"}
    ) == "ds_01XYZ"
    assert agol_sync.compute_service_name(
        {"title": None, "dataset_id": "ds_01ZZZ"}
    ) == "ds_01ZZZ"


# -----------------------------------------------------------------------------
# compute_item_properties — catalogue row → AGOL item dict
# -----------------------------------------------------------------------------

def _sample_row() -> dict[str, Any]:
    return {
        "dataset_id": "ds_01ABCDEFGHJKMNPQRSTVWXYZ12",
        "title": "Streams 2024",
        "summary": "Streams of the Y2Y region (2024 update).",
        "description": "Plain-text long-form description.",
        "tags": "streams;y2y;hydrology",
        "acknowledgements": "Y2Y team; Smith et al. 2024",
        "terms_of_use": "Internal use only.",
        "category": "Water",
    }


def test_compute_item_properties_maps_extrinsic_fields() -> None:
    props = agol_sync.compute_item_properties(_sample_row())
    assert props["title"] == "Streams 2024"
    assert props["snippet"] == "Streams of the Y2Y region (2024 update)."
    assert props["description"] == "Plain-text long-form description."
    assert props["accessInformation"] == "Y2Y team; Smith et al. 2024"
    assert props["licenseInfo"] == "Internal use only."


def test_compute_item_properties_splits_tags_on_semicolon() -> None:
    props = agol_sync.compute_item_properties(_sample_row())
    assert props["tags"] == ["streams", "y2y", "hydrology"]


def test_compute_item_properties_strips_tag_whitespace_and_drops_empties() -> None:
    row = _sample_row()
    row["tags"] = " streams ; ; y2y ;"
    props = agol_sync.compute_item_properties(row)
    assert props["tags"] == ["streams", "y2y"]


def test_compute_item_properties_assigns_category_as_full_display_name() -> None:
    row = _sample_row()
    row["category"] = "Jurisdictional & Political Boundaries"
    props = agol_sync.compute_item_properties(row)
    assert props["categories"] == ["Jurisdictional & Political Boundaries"]


def test_compute_item_properties_includes_subcategory_when_set() -> None:
    """Species rows with a subcategory tag with [parent, subcategory] so
    AGOL's nested category tree resolves both levels."""
    row = _sample_row()
    row["category"] = "Species"
    row["subcategory"] = "Grizzly Bear"
    props = agol_sync.compute_item_properties(row)
    assert props["categories"] == ["Species", "Grizzly Bear"]


def test_compute_item_properties_no_subcategory_means_single_entry() -> None:
    row = _sample_row()
    row["category"] = "Water"
    row["subcategory"] = None
    props = agol_sync.compute_item_properties(row)
    assert props["categories"] == ["Water"]


def test_compute_item_properties_stamps_type_keywords_for_y2y_discovery() -> None:
    props = agol_sync.compute_item_properties(_sample_row())
    assert "Y2Y" in props["typeKeywords"]
    assert "Y2Y:dataset_id:ds_01ABCDEFGHJKMNPQRSTVWXYZ12" in props["typeKeywords"]
    assert "Y2Y:category:Water" in props["typeKeywords"]


def test_compute_item_properties_tolerates_missing_optional_fields() -> None:
    row = {
        "dataset_id": "ds_xyz",
        "title": "Bare row",
        "tags": "",
        # everything else absent
    }
    props = agol_sync.compute_item_properties(row)
    assert props["title"] == "Bare row"
    assert props["snippet"] is None
    assert props["description"] is None
    assert props["tags"] == []
    assert "categories" not in props   # no category → no categories key
    assert props["typeKeywords"] == ["Y2Y", "Y2Y:dataset_id:ds_xyz"]


# -----------------------------------------------------------------------------
# get_gis / login_interactive — authentication paths
# -----------------------------------------------------------------------------

def test_get_gis_raises_auth_error_when_profile_missing(monkeypatch) -> None:
    """A nonexistent profile name surfaces as AgolAuthError, not a raw exception."""
    cfg = agol_config.AgolConfig(profile_name="this_profile_does_not_exist_for_y2y_test")
    with pytest.raises(agol_sync.AgolAuthError, match="agol-sync login"):
        agol_sync.get_gis(cfg)


def test_login_interactive_requires_client_id() -> None:
    cfg = agol_config.AgolConfig(client_id=None)
    with pytest.raises(agol_sync.AgolAuthError, match="client_id"):
        agol_sync.login_interactive(cfg)


# -----------------------------------------------------------------------------
# resolve_group_id — Conservation Atlas group lookup
# -----------------------------------------------------------------------------

def test_resolve_group_id_returns_cached_id_when_set() -> None:
    cfg = agol_config.AgolConfig(
        conservation_atlas_group_id="cached_group_xyz",
    )
    gis = MagicMock()  # never touched because cache hit
    assert agol_sync.resolve_group_id(gis, cfg) == "cached_group_xyz"
    gis.groups.search.assert_not_called()


def test_resolve_group_id_searches_and_caches_on_first_lookup(tmp_path, monkeypatch) -> None:
    cache_path = tmp_path / "group_cache.json"
    monkeypatch.setattr(agol_sync, "cache_group_id", lambda name, gid: cache_path.write_text(
        f'{{"{name}": "{gid}"}}'
    ))

    cfg = agol_config.AgolConfig(conservation_atlas_group_name="Y2Y Conservation Atlas")
    gis = MagicMock()
    group = MagicMock()
    group.title = "Y2Y Conservation Atlas"
    group.id = "live_lookup_group_id"
    gis.groups.search.return_value = [group]

    assert agol_sync.resolve_group_id(gis, cfg) == "live_lookup_group_id"
    gis.groups.search.assert_called_once()
    assert cache_path.exists()


def test_resolve_group_id_raises_when_group_absent() -> None:
    cfg = agol_config.AgolConfig(conservation_atlas_group_name="Nonexistent Group")
    gis = MagicMock()
    gis.groups.search.return_value = []
    with pytest.raises(agol_sync.AgolGroupNotFoundError, match="not found"):
        agol_sync.resolve_group_id(gis, cfg)


# -----------------------------------------------------------------------------
# ensure_org_categories — bootstrap the 10 typology categories
# -----------------------------------------------------------------------------

def _patch_schema_property(gis: MagicMock, initial: Any) -> list[Any]:
    """Install a property on the MagicMock's category-manager class so
    ``gis.content.categories.schema = ...`` records the writes.

    Returns a list that captures every write — tests inspect it.
    """
    state = {"value": initial}
    written: list[Any] = []
    type(gis.content.categories).schema = property(
        lambda self: state["value"],
        lambda self, value: (written.append(value), state.__setitem__("value", value))[0],
    )
    return written


def test_build_canonical_schema_matches_catalogue_typology() -> None:
    """Canonical schema mirrors taxonomy.CATEGORIES order with Species subcategories nested."""
    schema = agol_sync.build_canonical_schema()
    # Wrapped-root shape: [{"title": "Categories", "categories": [...]}]
    assert isinstance(schema, list)
    assert len(schema) == 1
    root = schema[0]
    assert root["title"] == "Categories"
    top_titles = [c["title"] for c in root["categories"]]
    # Exact order matches taxonomy.CATEGORIES
    from pipeline import taxonomy
    assert top_titles == list(taxonomy.CATEGORIES)
    # Species has 7 nested subcategories
    species_node = next(c for c in root["categories"] if c["title"] == "Species")
    sub_titles = [c["title"] for c in species_node["categories"]]
    assert set(sub_titles) == set(taxonomy.SUBCATEGORIES["Species"])
    # Other categories have empty subcategory lists
    for top in root["categories"]:
        if top["title"] != "Species":
            assert top["categories"] == []


def test_ensure_org_categories_writes_canonical_when_org_empty() -> None:
    cfg = agol_config.AgolConfig()
    gis = MagicMock()
    written = _patch_schema_property(gis, [{"title": "Categories", "categories": []}])

    diff = agol_sync.ensure_org_categories(gis, cfg, apply=True)

    # Wrote exactly once; result is the canonical schema.
    assert len(written) == 1
    assert written[0] == agol_sync.build_canonical_schema()
    assert diff.applied is True
    assert len(diff.will_add) == 10
    assert diff.will_orphan == []


def test_ensure_org_categories_orphans_old_categories() -> None:
    """Simulates the real Y2Y org's pre-2026 state and confirms the rewrite."""
    cfg = agol_config.AgolConfig()
    gis = MagicMock()
    old_schema = [{
        "title": "Categories",
        "categories": [
            {"title": "Administrative and Jurisdictional Boundaries", "categories": []},
            {"title": "Protected Areas and Conservation Lands", "categories": []},
            {"title": "Species and Species at Risk", "categories": [
                {"title": "Grizzly Bear", "categories": []},
            ]},
            {"title": "Water", "categories": []},
            {"title": "Climate Resilience", "categories": []},
        ],
    }]
    written = _patch_schema_property(gis, old_schema)

    diff = agol_sync.ensure_org_categories(gis, cfg, apply=True)

    assert diff.applied is True
    # 'Water' and 'Climate Resilience' carry over; the rest of the old
    # categories are orphaned.
    assert "Water" in diff.unchanged
    assert "Climate Resilience" in diff.unchanged
    assert "Administrative and Jurisdictional Boundaries" in diff.will_orphan
    assert "Protected Areas and Conservation Lands" in diff.will_orphan
    assert "Species and Species at Risk" in diff.will_orphan
    # All 10 catalogue categories minus the 2 already-present get added.
    assert "Jurisdictional & Political Boundaries" in diff.will_add
    assert "Human Dimensions" in diff.will_add
    assert "Species" in diff.will_add
    assert len(written) == 1


def test_ensure_org_categories_dry_run_does_not_write() -> None:
    cfg = agol_config.AgolConfig()
    gis = MagicMock()
    written = _patch_schema_property(gis, [{"title": "Categories", "categories": []}])

    diff = agol_sync.ensure_org_categories(gis, cfg, apply=False)

    assert diff.applied is False
    assert written == []
    # Diff content still computed.
    assert len(diff.will_add) == 10


def test_ensure_org_categories_is_noop_when_canonical_already_present() -> None:
    cfg = agol_config.AgolConfig()
    gis = MagicMock()
    written = _patch_schema_property(gis, agol_sync.build_canonical_schema())

    diff = agol_sync.ensure_org_categories(gis, cfg, apply=True)
    assert diff.applied is False  # no write needed
    assert diff.will_add == []
    assert diff.will_orphan == []
    assert written == []


# -----------------------------------------------------------------------------
# Reconciliation scope contract — catalogue-tracked items only.
#
# The integration must NOT scan the AGOL org's full content tree. Every
# sync/status/push/pull/reconcile operation iterates `datasets` rows; AGOL
# items not in the catalogue (maps, webapps, dashboards, the steward's
# personal items) are explicitly ignored. This contract is enforced by
# code structure — none of agol_sync.py's read paths call
# `gis.content.search()` over the whole org. The grep below documents
# that and serves as a regression-pin if a future change accidentally
# introduces org-wide content enumeration. See plan §"Reconciliation
# scope".
# -----------------------------------------------------------------------------

def test_agol_sync_module_does_not_enumerate_full_org_content() -> None:
    """agol_sync.py must never call `gis.content.search` without a
    catalogue-anchored item id query, since reconciliation is
    catalogue-centric. The token grep is the cheap regression pin.

    Allowed: `gis.content.get(item_id)`, `gis.groups.search(...)` for
    the Conservation Atlas group lookup. Not allowed: `gis.content.search()`
    over the whole org with no item-id constraint.
    """
    import inspect
    source = inspect.getsource(agol_sync)
    # No bare content.search calls. Phase A doesn't have any. If a
    # later phase adds one (e.g., a v2 audit command), it should be
    # gated by an explicit user-invoked CLI command, not auto-fired
    # by reconcile/status — and this test will need updating to
    # exclude that path.
    assert "gis.content.search" not in source, (
        "agol_sync.py grew a `gis.content.search()` call — confirm "
        "this isn't being invoked from a catalogue-iterating path "
        "(reconcile / status / push / pull). See plan §Reconciliation "
        "scope."
    )


# -----------------------------------------------------------------------------
# agol_config.load_config — env vars + YAML + defaults
# -----------------------------------------------------------------------------

def test_load_config_uses_defaults_with_empty_env(tmp_path) -> None:
    cfg = agol_config.load_config(
        yaml_path=tmp_path / "does_not_exist.yaml",
        env={},
        group_cache_path=tmp_path / "cache.json",
    )
    assert cfg.portal_url == "https://www.arcgis.com"
    assert cfg.profile_name == "y2y"
    assert cfg.conservation_atlas_group_name == "Y2Y Conservation Atlas"
    assert cfg.auto_push is True
    assert cfg.client_id is None
    assert cfg.conservation_atlas_group_id is None


def test_load_config_env_overrides(tmp_path) -> None:
    cfg = agol_config.load_config(
        yaml_path=tmp_path / "does_not_exist.yaml",
        env={
            "Y2Y_AGOL_CLIENT_ID": "abc123_oauth_id",
            "Y2Y_AGOL_AUTO_PUSH": "false",
            "Y2Y_AGOL_PROFILE": "y2y_test",
        },
        group_cache_path=tmp_path / "cache.json",
    )
    assert cfg.client_id == "abc123_oauth_id"
    assert cfg.auto_push is False
    assert cfg.profile_name == "y2y_test"


def test_load_config_yaml_overrides(tmp_path) -> None:
    yaml_path = tmp_path / "agol_config.yaml"
    yaml_path.write_text(
        "portal_url: https://my.custom.portal/arcgis\n"
        'conservation_atlas_group_name: My Custom Group\n'
    )
    cfg = agol_config.load_config(
        yaml_path=yaml_path,
        env={},
        group_cache_path=tmp_path / "cache.json",
    )
    assert cfg.portal_url == "https://my.custom.portal/arcgis"
    assert cfg.conservation_atlas_group_name == "My Custom Group"


def test_load_config_picks_up_cached_group_id(tmp_path) -> None:
    cache_path = tmp_path / "cache.json"
    cache_path.write_text('{"Y2Y Conservation Atlas": "cached_group_abc"}')
    cfg = agol_config.load_config(
        yaml_path=tmp_path / "missing.yaml",
        env={},
        group_cache_path=cache_path,
    )
    assert cfg.conservation_atlas_group_id == "cached_group_abc"


def test_load_config_rejects_unparseable_bool() -> None:
    with pytest.raises(RuntimeError, match="boolean"):
        agol_config.load_config(env={"Y2Y_AGOL_AUTO_PUSH": "maybe"})


def test_load_config_rejects_malformed_yaml(tmp_path) -> None:
    yaml_path = tmp_path / "bad.yaml"
    yaml_path.write_text("[ this : is not a mapping at top level ]")
    with pytest.raises(RuntimeError, match="YAML mapping"):
        agol_config.load_config(yaml_path=yaml_path, env={})


def test_cache_group_id_persists_and_can_be_reloaded(tmp_path) -> None:
    cache_path = tmp_path / "cache.json"
    agol_config.cache_group_id("Group A", "id_a", path=cache_path)
    agol_config.cache_group_id("Group B", "id_b", path=cache_path)
    # Read back via load_config
    cfg = agol_config.load_config(
        yaml_path=tmp_path / "missing.yaml",
        env={},
        group_cache_path=cache_path,
    )
    # load_config only looks up the configured group; verify both
    # are still present by reading the cache file directly.
    import json
    raw = json.loads(cache_path.read_text())
    assert raw == {"Group A": "id_a", "Group B": "id_b"}


# ----------------------------------------------------------------------------
# Phase C: adoption
# ----------------------------------------------------------------------------

from pipeline import inventory_manager


@pytest.fixture
def _config_no_cache(tmp_path: Path) -> agol_config.AgolConfig:
    """An AgolConfig with the Conservation Atlas group ID
    pre-populated so resolve_group_id() doesn't need to call AGOL.
    Mirrors the fixture in test_agol_push.py."""
    return agol_config.AgolConfig(
        conservation_atlas_group_id="cached_group_xyz",
    )


def _adoption_row(dataset_id: str = "ds_adopt_test") -> dict:
    """Row dict for a pre-existing AGOL item (agol_item_id set,
    sync_status='unpublished'). Reuses test_agol_push._full_row to
    stay schema-current."""
    from tests.test_agol_push import _full_row
    row = _full_row(
        dataset_id=dataset_id,
        file_path="Juris_Political_Boundaries/fortress_mountain.gpkg",
        category="Jurisdictional & Political Boundaries",
        agol_item_id="ad594173963245388b45bd2a123f9466",
        sync_status="unpublished",
    )
    # _full_row defaults: title='Test Title', summary='Summary.',
    # description='Description.', tags='test;y2y',
    # acknowledgements='Ack.', terms_of_use='TOU.'.
    return row


def _make_agol_item(
    *,
    # Defaults match _full_row's defaults so a vanilla
    # _make_agol_item() represents an AGOL item that field-for-field
    # matches the catalogue row built by _adoption_row().
    title="Test Title",
    snippet="Summary.",
    description="Description.",
    tags=("test", "y2y"),
    access_information="Ack.",
    license_info="TOU.",
    categories=("Jurisdictional & Political Boundaries",),
    created=1700000000000,
) -> MagicMock:
    """A MagicMock that looks like an arcgis.gis.Item with the
    fields adopt_row reads."""
    item = MagicMock()
    item.title = title
    item.snippet = snippet
    item.description = description
    item.tags = list(tags)
    item.accessInformation = access_information
    item.licenseInfo = license_info
    item.categories = list(categories)
    item.created = created
    return item


def test_adopt_row_marks_clean_when_agol_matches_catalogue(
    project_tree, _config_no_cache,
) -> None:
    """When AGOL field-for-field matches catalogue, adoption flips
    sync_status to 'clean' and populates last_synced_at +
    agol_published_at."""
    db = project_tree["db"]
    row = _adoption_row()
    inventory_manager.insert_dataset(db, row)

    # Default _make_agol_item already matches the catalogue row
    # built by _adoption_row() field-for-field — no overrides needed.
    item = _make_agol_item()
    gis = MagicMock()
    gis.content.get.return_value = item

    result = agol_sync.adopt_row(
        db, row["dataset_id"], gis, _config_no_cache, actor="tester",
    )

    assert result.sync_status_after == "clean"
    assert result.sync_status_before == "unpublished"
    assert result.error is None

    fresh = inventory_manager.get_dataset(db, row["dataset_id"])
    assert fresh["sync_status"] == "clean"
    assert fresh["last_synced_at"] is not None
    assert fresh["agol_published_at"] is not None


def test_adopt_row_marks_conflict_when_agol_differs(
    project_tree, _config_no_cache,
) -> None:
    """When any field differs between AGOL and catalogue, adoption
    flips sync_status to 'conflict' and writes a structured diff
    to changelog. Adoption never mutates AGOL."""
    db = project_tree["db"]
    row = _adoption_row()
    inventory_manager.insert_dataset(db, row)

    # AGOL has a different title than the catalogue (catalogue =
    # 'Test Title' from _full_row default; AGOL = 'Old AGOL Title').
    item = _make_agol_item(
        title="Old AGOL Title",
        snippet="Summary.",
        description="Description.",
        tags=["test", "y2y"],
        access_information="Ack.",
        license_info="TOU.",
        categories=["Jurisdictional & Political Boundaries"],
    )
    gis = MagicMock()
    gis.content.get.return_value = item

    result = agol_sync.adopt_row(
        db, row["dataset_id"], gis, _config_no_cache, actor="tester",
    )

    assert result.sync_status_after == "conflict"

    fresh = inventory_manager.get_dataset(db, row["dataset_id"])
    assert fresh["sync_status"] == "conflict"

    # The changelog entry captures the structured per-field diff.
    log = inventory_manager.load_changelog(db)
    relevant = [
        r for r in log
        if r["dataset_id"] == row["dataset_id"]
        and r["field_changed"] == "sync_status"
        and r["new_value"] == "conflict"
    ]
    assert len(relevant) == 1
    note = relevant[0]["note"] or ""
    assert "title" in note
    assert "Old AGOL Title" in note  # AGOL side
    assert "Test Title" in note      # catalogue side


def test_adopt_row_marks_error_when_agol_item_missing(
    project_tree, _config_no_cache,
) -> None:
    """If gis.content.get() returns None (item deleted out-of-band),
    adoption marks the row 'error' with a remediation hint in
    internal_notes pointing at unpublish + re-push."""
    db = project_tree["db"]
    row = _adoption_row()
    inventory_manager.insert_dataset(db, row)

    gis = MagicMock()
    gis.content.get.return_value = None  # AGOL item missing

    result = agol_sync.adopt_row(
        db, row["dataset_id"], gis, _config_no_cache, actor="tester",
    )

    assert result.sync_status_after == "error"
    assert result.error is not None
    assert "no longer exists" in result.error.lower()

    fresh = inventory_manager.get_dataset(db, row["dataset_id"])
    assert fresh["sync_status"] == "error"
    notes = fresh["internal_notes"] or ""
    assert "[agol]" in notes
    assert "no longer exists" in notes


def test_adopt_row_rejects_row_without_agol_item_id(
    project_tree, _config_no_cache,
) -> None:
    """Adoption only applies to rows with a pre-existing
    agol_item_id. Rows without one are nonsense to 'adopt'."""
    db = project_tree["db"]
    row = _adoption_row()
    row["agol_item_id"] = None
    inventory_manager.insert_dataset(db, row)
    gis = MagicMock()

    with pytest.raises(agol_sync.AgolError, match="no agol_item_id"):
        agol_sync.adopt_row(
            db, row["dataset_id"], gis, _config_no_cache, actor="tester",
        )


def test_adopt_row_rejects_row_already_under_management(
    project_tree, _config_no_cache,
) -> None:
    """Only sync_status='unpublished' rows are eligible. A row that
    already went through adoption (or push) is rejected with a
    clear message."""
    db = project_tree["db"]
    row = _adoption_row()
    row["sync_status"] = "clean"
    inventory_manager.insert_dataset(db, row)
    gis = MagicMock()

    with pytest.raises(agol_sync.AgolError, match="sync_status is 'clean'"):
        agol_sync.adopt_row(
            db, row["dataset_id"], gis, _config_no_cache, actor="tester",
        )


def test_adopt_row_normalises_tags_as_set_comparison(
    project_tree, _config_no_cache,
) -> None:
    """Tags compare set-equality, not list-order. Catalogue stores
    ';'-delimited strings; AGOL stores a list. The same logical
    set should adopt clean regardless of order."""
    db = project_tree["db"]
    row = _adoption_row()
    inventory_manager.insert_dataset(db, row)

    # AGOL has tags in reverse order — should still match.
    item = _make_agol_item(
        tags=["y2y", "test"],  # catalogue tags is 'test;y2y'
    )
    gis = MagicMock()
    gis.content.get.return_value = item

    result = agol_sync.adopt_row(
        db, row["dataset_id"], gis, _config_no_cache, actor="tester",
    )
    assert result.sync_status_after == "clean"


# =============================================================================
# Auto-sync hooks (Phase C.2)
# =============================================================================

# ----- inventory_manager._maybe_mark_dirty -----------------------------------

def test_maybe_mark_dirty_promotes_clean_to_pending_push(project_tree) -> None:
    """The clean → pending_push transition is the auto-sync entry point.

    Any catalogue mutation on a 'clean' row signals the integration
    that AGOL is now out of date and a push is owed.
    """
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_dirty_test", file_path="Water/x.gpkg",
        sync_status="clean",
        agol_item_id="abc123", agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    result = inventory_manager._maybe_mark_dirty(
        db, row["dataset_id"], actor="tester", trigger="update",
    )
    assert result == "pending_push"

    fresh = inventory_manager.get_dataset(db, row["dataset_id"])
    assert fresh["sync_status"] == "pending_push"

    # A changelog entry captures the auto-mark.
    log = inventory_manager.load_changelog(db)
    auto_marks = [r for r in log if "auto-marked pending_push" in (r["note"] or "")]
    assert len(auto_marks) == 1
    assert auto_marks[0]["field_changed"] == "sync_status"
    assert auto_marks[0]["old_value"] == "clean"
    assert auto_marks[0]["new_value"] == "pending_push"


@pytest.mark.parametrize("prior_status", [
    "unpublished", "pending_push", "pending_pull", "conflict", "error",
])
def test_maybe_mark_dirty_leaves_non_clean_rows_alone(
    project_tree, prior_status,
) -> None:
    """Only 'clean' rows transition. Anything else stays put — pulls,
    conflicts, and existing pending_push/error states all encode
    steward-relevant information that auto-marking would erase."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_dirty_skip", file_path="Water/x.gpkg",
        sync_status=prior_status,
        agol_item_id="abc123", agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    result = inventory_manager._maybe_mark_dirty(
        db, row["dataset_id"], actor="tester", trigger="update",
    )
    assert result is None

    fresh = inventory_manager.get_dataset(db, row["dataset_id"])
    assert fresh["sync_status"] == prior_status


def test_maybe_mark_dirty_tolerates_missing_row(project_tree) -> None:
    """A typo'd dataset_id is a no-op, not a crash. Lifecycle callers
    rely on this being safe to call unconditionally after every
    mutation."""
    from pipeline import inventory_manager
    assert inventory_manager._maybe_mark_dirty(
        project_tree["db"], "ds_does_not_exist",
        actor="tester", trigger="update",
    ) is None


# ----- agol_sync.try_auto_push -----------------------------------------------

def test_try_auto_push_skips_when_auto_push_disabled(
    project_tree, monkeypatch,
) -> None:
    """If the steward sets Y2Y_AGOL_AUTO_PUSH=false, the hook returns
    immediately without touching AGOL — even for pushable rows."""
    monkeypatch.setenv("Y2Y_AGOL_AUTO_PUSH", "false")
    # Even if push() would raise, we should never get there.
    monkeypatch.setattr(agol_sync, "push", MagicMock(side_effect=AssertionError(
        "push should not be called when auto_push is disabled"
    )))

    result = agol_sync.try_auto_push(
        project_tree["db"], "ds_anything",
        library_root=project_tree["library"],
        actor="tester", trigger="update",
    )
    assert result is None


def test_try_auto_push_skips_when_row_not_pushable(
    project_tree, monkeypatch,
) -> None:
    """Rows in 'pending_pull' / 'conflict' / 'error' are blocked from
    auto-push — they need manual resolution. The hook silently skips."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    monkeypatch.setenv("Y2Y_AGOL_AUTO_PUSH", "true")
    monkeypatch.setattr(agol_sync, "push", MagicMock(side_effect=AssertionError(
        "push should not be called for non-pushable status"
    )))

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_blocked", file_path="Water/x.gpkg",
        sync_status="conflict",
        agol_item_id="abc123", agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    result = agol_sync.try_auto_push(
        db, row["dataset_id"],
        library_root=project_tree["library"], actor="tester", trigger="update",
    )
    assert result is None


def test_try_auto_push_skips_when_agol_format_unset(
    project_tree, monkeypatch,
) -> None:
    """A row with sync_status='unpublished' but no agol_format pinned
    has no meaningful target — skip without contacting AGOL."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    monkeypatch.setenv("Y2Y_AGOL_AUTO_PUSH", "true")
    monkeypatch.setattr(agol_sync, "push", MagicMock(side_effect=AssertionError(
        "push should not be called when agol_format is unset"
    )))

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_no_target", file_path="Water/x.gpkg",
        sync_status="unpublished",
        agol_format=None,
    )
    inventory_manager.insert_dataset(db, row)

    result = agol_sync.try_auto_push(
        db, row["dataset_id"],
        library_root=project_tree["library"], actor="tester", trigger="update",
    )
    assert result is None


def test_try_auto_push_swallows_gis_connection_failure(
    project_tree, monkeypatch,
) -> None:
    """If get_gis() raises (no profile, offline, expired tokens), the
    hook writes a deferred-push changelog entry and returns None. The
    catalogue row is unchanged."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    monkeypatch.setenv("Y2Y_AGOL_AUTO_PUSH", "true")
    monkeypatch.setattr(
        agol_sync, "get_gis",
        MagicMock(side_effect=agol_sync.AgolAuthError("no profile")),
    )

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_offline", file_path="Water/x.gpkg",
        sync_status="pending_push",
        agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    result = agol_sync.try_auto_push(
        db, row["dataset_id"],
        library_root=project_tree["library"], actor="tester", trigger="update",
    )
    assert result is None

    # Row's sync_status is unchanged.
    fresh = inventory_manager.get_dataset(db, row["dataset_id"])
    assert fresh["sync_status"] == "pending_push"

    # Changelog records the deferred attempt.
    log = inventory_manager.load_changelog(db)
    deferred = [r for r in log if "auto-push deferred" in (r["note"] or "")]
    assert len(deferred) == 1
    assert "AgolAuthError" in deferred[0]["note"]


def test_try_auto_push_swallows_push_failure(
    project_tree, monkeypatch,
) -> None:
    """If push() raises mid-flight (AGOL 5xx, validation error), the
    hook catches the exception, writes an audit changelog entry, and
    returns None. The catalogue mutation that triggered the hook is
    NOT rolled back — auto-sync is best-effort."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    monkeypatch.setenv("Y2Y_AGOL_AUTO_PUSH", "true")
    monkeypatch.setattr(agol_sync, "get_gis", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(
        agol_sync, "push",
        MagicMock(side_effect=agol_sync.AgolError("AGOL returned 503")),
    )

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_push_fail", file_path="Water/x.gpkg",
        sync_status="pending_push",
        agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    result = agol_sync.try_auto_push(
        db, row["dataset_id"],
        library_root=project_tree["library"], actor="tester", trigger="update",
    )
    assert result is None

    log = inventory_manager.load_changelog(db)
    failures = [r for r in log if "auto-push attempt" in (r["note"] or "") and
                "failed" in (r["note"] or "")]
    assert len(failures) == 1
    assert "AGOL returned 503" in failures[0]["note"]


def test_try_auto_push_fires_push_when_everything_is_set_up(
    project_tree, monkeypatch,
) -> None:
    """The happy path: row is pushable, agol_format set, GIS connects,
    push() returns a SyncResult. The hook returns it."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    monkeypatch.setenv("Y2Y_AGOL_AUTO_PUSH", "true")
    monkeypatch.setattr(agol_sync, "get_gis", MagicMock(return_value=MagicMock()))
    fake_result = agol_sync.SyncResult(
        dataset_id="ds_happy",
        action="push",
        sync_status_before="pending_push",
        sync_status_after="clean",
        agol_item_id="abc123",
        note="ok",
    )
    push_mock = MagicMock(return_value=fake_result)
    monkeypatch.setattr(agol_sync, "push", push_mock)

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_happy", file_path="Water/x.gpkg",
        sync_status="pending_push",
        agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    result = agol_sync.try_auto_push(
        db, row["dataset_id"],
        library_root=project_tree["library"], actor="tester", trigger="update",
    )
    assert result is fake_result
    push_mock.assert_called_once()


# ----- lifecycle.update integration -------------------------------------------

def test_lifecycle_update_auto_marks_pending_push_on_clean_row(
    project_tree, populate_dataset, monkeypatch,
) -> None:
    """End-to-end: a steward edits a 'clean' row → sync_status moves
    to 'pending_push' automatically. Auto-push attempt is gated by
    a separate flag; this test asserts the dirty-mark itself."""
    from pipeline import inventory_manager, lifecycle

    # Make sure no actual push happens.
    monkeypatch.setenv("Y2Y_AGOL_AUTO_PUSH", "false")

    db = project_tree["db"]
    # populate_dataset is a callable factory — invoke it to scan + approve
    # a fresh GPKG and produce a 'unpublished' row. Then promote to 'clean'
    # so we can observe the auto-mark trigger.
    dataset_id, _ = populate_dataset()
    inventory_manager.update_dataset(db, dataset_id, {
        "sync_status": "clean", "agol_item_id": "abc123",
    })

    lifecycle.update(
        db, dataset_id=dataset_id,
        fields={"summary": "Revised summary text"},
        actor="tester",
    )

    fresh = inventory_manager.get_dataset(db, dataset_id)
    assert fresh["sync_status"] == "pending_push"


# =============================================================================
# reconcile_bidirectional (Phase C.3)
# =============================================================================

def test_reconcile_pushes_pending_push_rows(
    project_tree, _config_no_cache, monkeypatch,
) -> None:
    """A 'pending_push' row gets push()'d. On success it lands in
    the 'pushed' bucket with sync_status_after='clean'."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_pp", file_path="Water/x.gpkg",
        sync_status="pending_push", agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    fake_result = agol_sync.SyncResult(
        dataset_id="ds_pp", action="push",
        sync_status_before="pending_push", sync_status_after="clean",
        agol_item_id="new123", note="published",
    )
    monkeypatch.setattr(agol_sync, "push", MagicMock(return_value=fake_result))

    report = agol_sync.reconcile_bidirectional(
        db, MagicMock(), _config_no_cache,
        library_root=project_tree["library"], actor="reconcile-cron",
        reports_dir=project_tree["root"] / "reports",
    )

    assert report.counts_by_bucket.get("pushed") == 1
    assert report.outcomes[0].sync_status_after == "clean"
    assert report.report_path.exists()
    text = report.report_path.read_text()
    assert "ds_pp" in text
    assert "pushed" in text.lower()


def test_reconcile_marks_clean_row_pending_pull_when_agol_drifted(
    project_tree, _config_no_cache,
) -> None:
    """A 'clean' row whose AGOL item.modified is newer than
    last_synced_at is flagged pending_pull, with a changelog entry."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_drift", file_path="Water/x.gpkg",
        sync_status="clean", agol_item_id="item123",
        agol_format="feature-layer",
    )
    row["last_synced_at"] = "2026-01-01T00:00:00Z"
    inventory_manager.insert_dataset(db, row)

    # Item.modified must be parseable as ms-epoch and resolve to an
    # ISO-8601 string AFTER 2026-01-01.
    item = MagicMock()
    # 2026-06-01 00:00 UTC in milliseconds
    item.modified = 1780272000000

    gis = MagicMock()
    gis.content.get.return_value = item

    report = agol_sync.reconcile_bidirectional(
        db, gis, _config_no_cache,
        library_root=project_tree["library"], actor="reconcile-cron",
        reports_dir=project_tree["root"] / "reports",
    )

    fresh = inventory_manager.get_dataset(db, "ds_drift")
    assert fresh["sync_status"] == "pending_pull"
    assert report.counts_by_bucket.get("pulled_flag") == 1

    log = inventory_manager.load_changelog(db)
    flagged = [r for r in log if "reconcile flagged AGOL drift" in (r["note"] or "")]
    assert len(flagged) == 1


def test_reconcile_leaves_clean_row_alone_when_agol_in_sync(
    project_tree, _config_no_cache,
) -> None:
    """A 'clean' row whose AGOL.modified is older than last_synced_at
    stays clean. Outcome lands in 'clean_confirmed'."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_stable", file_path="Water/x.gpkg",
        sync_status="clean", agol_item_id="item123",
        agol_format="feature-layer",
    )
    row["last_synced_at"] = "2026-06-01T00:00:00Z"
    inventory_manager.insert_dataset(db, row)

    item = MagicMock()
    # 2026-01-01 00:00 UTC in milliseconds — older than last_synced_at
    item.modified = 1767225600000

    gis = MagicMock()
    gis.content.get.return_value = item

    report = agol_sync.reconcile_bidirectional(
        db, gis, _config_no_cache,
        library_root=project_tree["library"], actor="reconcile-cron",
        reports_dir=project_tree["root"] / "reports",
    )

    fresh = inventory_manager.get_dataset(db, "ds_stable")
    assert fresh["sync_status"] == "clean"
    assert report.counts_by_bucket.get("clean_confirmed") == 1


def test_reconcile_marks_failed_push_as_error(
    project_tree, _config_no_cache, monkeypatch,
) -> None:
    """A 'pending_push' row whose push() raises AgolError gets
    sync_status='error' and lands in the 'push_failed' bucket."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_fail", file_path="Water/x.gpkg",
        sync_status="pending_push", agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    monkeypatch.setattr(
        agol_sync, "push",
        MagicMock(side_effect=agol_sync.AgolError("AGOL returned 503")),
    )

    report = agol_sync.reconcile_bidirectional(
        db, MagicMock(), _config_no_cache,
        library_root=project_tree["library"], actor="reconcile-cron",
        reports_dir=project_tree["root"] / "reports",
    )

    fresh = inventory_manager.get_dataset(db, "ds_fail")
    assert fresh["sync_status"] == "error"
    assert report.counts_by_bucket.get("push_failed") == 1


def test_reconcile_retries_error_rows_once(
    project_tree, _config_no_cache, monkeypatch,
) -> None:
    """A row stuck in 'error' gets one retry attempt. Success →
    'error_retry_ok' bucket and sync_status='clean'."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_recovers", file_path="Water/x.gpkg",
        sync_status="error", agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    fake_result = agol_sync.SyncResult(
        dataset_id="ds_recovers", action="push",
        sync_status_before="pending_push", sync_status_after="clean",
        agol_item_id="new123", note="published",
    )
    monkeypatch.setattr(agol_sync, "push", MagicMock(return_value=fake_result))

    report = agol_sync.reconcile_bidirectional(
        db, MagicMock(), _config_no_cache,
        library_root=project_tree["library"], actor="reconcile-cron",
        reports_dir=project_tree["root"] / "reports",
    )

    assert report.counts_by_bucket.get("error_retry_ok") == 1


def test_reconcile_skips_conflict_and_pending_pull(
    project_tree, _config_no_cache,
) -> None:
    """Rows in 'conflict' or 'pending_pull' are reconcile-out-of-scope;
    they need Phase D pull. Skipped with explanatory notes."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    for did, status, item_id in (
        ("ds_c", "conflict", "abc1"),
        ("ds_pp_pull", "pending_pull", "abc2"),
    ):
        row = _full_row(
            dataset_id=did, file_path="Water/x.gpkg",
            sync_status=status, agol_item_id=item_id,
            agol_format="feature-layer",
        )
        inventory_manager.insert_dataset(db, row)

    report = agol_sync.reconcile_bidirectional(
        db, MagicMock(), _config_no_cache,
        library_root=project_tree["library"], actor="reconcile-cron",
        reports_dir=project_tree["root"] / "reports",
    )

    assert report.counts_by_bucket.get("skipped") == 2


def test_reconcile_dry_run_does_not_mutate_catalogue(
    project_tree, _config_no_cache, monkeypatch,
) -> None:
    """Dry-run reports planned actions but does not push or mutate
    sync_status. Useful for previewing the impact of a scheduled run."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_dryrun", file_path="Water/x.gpkg",
        sync_status="pending_push", agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    push_mock = MagicMock(side_effect=AssertionError("dry-run should not push"))
    monkeypatch.setattr(agol_sync, "push", push_mock)

    report = agol_sync.reconcile_bidirectional(
        db, MagicMock(), _config_no_cache,
        library_root=project_tree["library"], actor="reconcile-cron",
        reports_dir=project_tree["root"] / "reports",
        dry_run=True,
    )

    fresh = inventory_manager.get_dataset(db, "ds_dryrun")
    assert fresh["sync_status"] == "pending_push"
    assert report.counts_by_bucket.get("skipped") == 1
    push_mock.assert_not_called()


def test_reconcile_writes_report_even_when_no_rows(
    project_tree, _config_no_cache,
) -> None:
    """Empty catalogue → still produces a report (audit trail) noting
    no active rows."""
    report = agol_sync.reconcile_bidirectional(
        project_tree["db"], MagicMock(), _config_no_cache,
        library_root=project_tree["library"], actor="reconcile-cron",
        reports_dir=project_tree["root"] / "reports",
    )

    assert report.report_path.exists()
    assert report.outcomes == []
    assert "no active rows" in report.report_path.read_text()


# =============================================================================
# pull / pull_all_pending / detect_pull_candidates (Phase D)
# =============================================================================

def _make_drifted_agol_item(
    *,
    title: str | None = None,
    snippet: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    accessInformation: str | None = None,
    licenseInfo: str | None = None,
    categories: list[str] | None = None,
    modified: int | None = None,
) -> MagicMock:
    """Build a MagicMock AGOL item with the supplied attributes.

    Defaults to values that match _full_row's defaults so the steward
    can override only the fields they want to test drifting.
    """
    item = MagicMock()
    item.title = title if title is not None else "Test Title"
    item.snippet = snippet if snippet is not None else "Summary."
    item.description = description if description is not None else "Description."
    item.tags = tags if tags is not None else ["test", "y2y"]
    item.accessInformation = (
        accessInformation if accessInformation is not None else "Ack."
    )
    item.licenseInfo = licenseInfo if licenseInfo is not None else "TOU."
    item.categories = categories if categories is not None else ["Water"]
    if modified is not None:
        item.modified = modified
    return item


def test_pull_no_drift_marks_clean_and_bumps_last_synced(
    project_tree, _config_no_cache,
) -> None:
    """A pull on a row whose AGOL state matches the catalogue is a
    no-op resolution: marks the row 'clean' and bumps last_synced_at.
    Useful as a 'confirm sync state' command."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_nodrift", file_path="Water/x.gpkg",
        sync_status="pending_pull", agol_item_id="abc",
        agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    item = _make_drifted_agol_item()  # all defaults match _full_row
    gis = MagicMock()
    gis.content.get.return_value = item

    result = agol_sync.pull(
        db, "ds_nodrift", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
    )

    assert result.sync_status_after == "clean"
    fresh = inventory_manager.get_dataset(db, "ds_nodrift")
    assert fresh["sync_status"] == "clean"
    assert fresh["last_synced_at"] is not None


def test_pull_surface_mode_marks_conflict_and_logs_diff(
    project_tree, _config_no_cache,
) -> None:
    """Default pull (no resolution) on a drifted row: marks
    sync_status='conflict', logs structured per-field diff to the
    changelog. Does NOT modify either catalogue text fields or AGOL."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_drift", file_path="Water/x.gpkg",
        sync_status="pending_pull", agol_item_id="abc",
        agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    # AGOL drifted on title + snippet.
    item = _make_drifted_agol_item(
        title="New AGOL Title", snippet="AGOL snippet",
    )
    gis = MagicMock()
    gis.content.get.return_value = item

    result = agol_sync.pull(
        db, "ds_drift", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
    )

    assert result.sync_status_after == "conflict"
    fresh = inventory_manager.get_dataset(db, "ds_drift")
    assert fresh["sync_status"] == "conflict"
    # Catalogue text fields untouched.
    assert fresh["title"] == "Test Title"
    assert fresh["summary"] == "Summary."

    log = inventory_manager.load_changelog(db)
    diffs = [r for r in log if "pull surfaced" in (r["note"] or "")]
    assert len(diffs) == 1
    assert "title:" in diffs[0]["note"]
    assert "snippet:" in diffs[0]["note"]
    assert "New AGOL Title" in diffs[0]["note"]


def test_pull_accept_absorbs_agol_text_fields(
    project_tree, _config_no_cache,
) -> None:
    """pull --accept absorbs AGOL's title/summary/description/tags/
    acknowledgements/terms_of_use into the catalogue. Row ends 'clean'."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_absorb", file_path="Water/x.gpkg",
        sync_status="conflict", agol_item_id="abc",
        agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    item = _make_drifted_agol_item(
        title="New AGOL Title",
        snippet="New AGOL snippet",
        description="New AGOL description",
        tags=["new", "agol-tags"],
        accessInformation="New AGOL ack",
        licenseInfo="New AGOL terms",
    )
    gis = MagicMock()
    gis.content.get.return_value = item

    result = agol_sync.pull(
        db, "ds_absorb", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        resolution="accept_agol",
    )

    assert result.sync_status_after == "clean"
    fresh = inventory_manager.get_dataset(db, "ds_absorb")
    assert fresh["title"] == "New AGOL Title"
    assert fresh["summary"] == "New AGOL snippet"
    assert fresh["description"] == "New AGOL description"
    # Tags are sorted by _diff_adoption_fields before write-back.
    assert set(fresh["tags"].split(";")) == {"new", "agol-tags"}
    assert fresh["acknowledgements"] == "New AGOL ack"
    assert fresh["terms_of_use"] == "New AGOL terms"
    assert fresh["last_synced_at"] is not None


def test_pull_accept_skips_categories_with_internal_notes(
    project_tree, _config_no_cache,
) -> None:
    """The `categories` diff is filesystem-bound (folder location
    dictates it) so pull --accept skips it and surfaces a steward
    note via internal_notes — change it via `y2y rename`."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_cat", file_path="Water/x.gpkg",
        sync_status="conflict", agol_item_id="abc",
        agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    # Only categories drifted.
    item = _make_drifted_agol_item(categories=["Species"])
    gis = MagicMock()
    gis.content.get.return_value = item

    result = agol_sync.pull(
        db, "ds_cat", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        resolution="accept_agol",
    )

    assert result.sync_status_after == "clean"
    fresh = inventory_manager.get_dataset(db, "ds_cat")
    # Catalogue's category column is FS-bound — NOT mutated by pull.
    assert fresh["category"] == "Water"
    # internal_notes annotation about the skipped diff.
    assert fresh["internal_notes"] is not None
    assert "categories" in fresh["internal_notes"]
    assert "y2y rename" in fresh["internal_notes"]


def test_pull_reject_repushes_catalogue_to_agol(
    project_tree, _config_no_cache, monkeypatch,
) -> None:
    """pull --reject flips sync_status to pending_push and calls
    push() to overwrite AGOL. The push handles all bookkeeping."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_reject", file_path="Water/x.gpkg",
        sync_status="conflict", agol_item_id="abc",
        agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    item = _make_drifted_agol_item(title="AGOL drifted title")
    gis = MagicMock()
    gis.content.get.return_value = item

    fake_push_result = agol_sync.SyncResult(
        dataset_id="ds_reject", action="push",
        sync_status_before="pending_push", sync_status_after="clean",
        agol_item_id="abc", note="re-pushed",
    )
    push_mock = MagicMock(return_value=fake_push_result)
    monkeypatch.setattr(agol_sync, "push", push_mock)

    result = agol_sync.pull(
        db, "ds_reject", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
        resolution="reject_agol",
    )

    assert result.sync_status_after == "clean"
    push_mock.assert_called_once()
    # The row was flipped to pending_push so push() would accept it.
    log = inventory_manager.load_changelog(db)
    rejects = [r for r in log if "pull --reject" in (r["note"] or "")]
    assert len(rejects) == 1


def test_pull_marks_error_when_agol_item_missing(
    project_tree, _config_no_cache,
) -> None:
    """A deleted-out-of-band AGOL item produces an actionable error
    outcome: sync_status='error', internal_notes annotated, steward
    can unpublish or re-push."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_gone", file_path="Water/x.gpkg",
        sync_status="pending_pull", agol_item_id="abc",
        agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    gis = MagicMock()
    gis.content.get.return_value = None

    result = agol_sync.pull(
        db, "ds_gone", gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
    )

    assert result.sync_status_after == "error"
    assert result.error is not None
    fresh = inventory_manager.get_dataset(db, "ds_gone")
    assert fresh["sync_status"] == "error"
    assert "no longer exists" in (fresh["internal_notes"] or "")


def test_pull_rejects_non_pullable_state(
    project_tree, _config_no_cache,
) -> None:
    """pull refuses unpublished / pending_push / error. Steward
    must push or unpublish those first."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_unp", file_path="Water/x.gpkg",
        sync_status="unpublished", agol_item_id="abc",
        agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    with pytest.raises(agol_sync.AgolError, match="sync_status is 'unpublished'"):
        agol_sync.pull(
            db, "ds_unp", MagicMock(), _config_no_cache,
            library_root=project_tree["library"], actor="tester",
        )


def test_pull_rejects_row_without_agol_item_id(
    project_tree, _config_no_cache,
) -> None:
    """Can't pull from a row that has no AGOL link — sanity check."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_noid", file_path="Water/x.gpkg",
        sync_status="conflict", agol_item_id=None,
        agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    with pytest.raises(agol_sync.AgolError, match="no agol_item_id"):
        agol_sync.pull(
            db, "ds_noid", MagicMock(), _config_no_cache,
            library_root=project_tree["library"], actor="tester",
        )


def test_pull_rejects_unknown_resolution_value(
    project_tree, _config_no_cache,
) -> None:
    """Defensive — anything other than None / 'accept_agol' /
    'reject_agol' should be rejected with a clear error so a typo
    in a future caller doesn't silently fall through to surface mode."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    row = _full_row(
        dataset_id="ds_bad_res", file_path="Water/x.gpkg",
        sync_status="conflict", agol_item_id="abc",
        agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row)

    with pytest.raises(agol_sync.AgolError, match="unknown pull resolution"):
        agol_sync.pull(
            db, "ds_bad_res", MagicMock(), _config_no_cache,
            library_root=project_tree["library"], actor="tester",
            resolution="accept",  # missing _agol suffix
        )


def test_pull_all_pending_surfaces_each_row(
    project_tree, _config_no_cache,
) -> None:
    """Batch mode iterates pending_pull rows, calls pull() with no
    resolution on each, returns one SyncResult per row."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    for did, item_id in (("ds_p1", "agol1"), ("ds_p2", "agol2")):
        row = _full_row(
            dataset_id=did, file_path=f"Water/{did}.gpkg",
            sync_status="pending_pull", agol_item_id=item_id,
            agol_format="feature-layer",
        )
        inventory_manager.insert_dataset(db, row)
    # A non-pending_pull row should NOT be picked up.
    row3 = _full_row(
        dataset_id="ds_clean", file_path="Water/clean.gpkg",
        sync_status="clean", agol_item_id="agol3",
        agol_format="feature-layer",
    )
    inventory_manager.insert_dataset(db, row3)

    gis = MagicMock()
    gis.content.get.return_value = _make_drifted_agol_item(title="Drifted")

    results = agol_sync.pull_all_pending(
        db, gis, _config_no_cache,
        library_root=project_tree["library"], actor="tester",
    )

    assert len(results) == 2
    assert {r.dataset_id for r in results} == {"ds_p1", "ds_p2"}
    for r in results:
        assert r.sync_status_after == "conflict"


def test_detect_pull_candidates_returns_drifted_clean_rows(
    project_tree, _config_no_cache,
) -> None:
    """detect_pull_candidates is the read-only equivalent of the
    reconcile pulled_flag bucket: returns clean rows whose
    AGOL.modified > last_synced_at, without mutating anything."""
    from pipeline import inventory_manager
    from tests.test_agol_push import _full_row

    db = project_tree["db"]
    drifted = _full_row(
        dataset_id="ds_drift", file_path="Water/x.gpkg",
        sync_status="clean", agol_item_id="abc",
        agol_format="feature-layer",
    )
    drifted["last_synced_at"] = "2026-01-01T00:00:00Z"
    inventory_manager.insert_dataset(db, drifted)
    stable = _full_row(
        dataset_id="ds_stable", file_path="Water/y.gpkg",
        sync_status="clean", agol_item_id="def",
        agol_format="feature-layer",
    )
    stable["last_synced_at"] = "2026-06-01T00:00:00Z"
    inventory_manager.insert_dataset(db, stable)

    def _get(item_id):
        # 2026-06-01 for both items — newer than ds_drift's last_synced,
        # older than ds_stable's.
        item = _make_drifted_agol_item(modified=1780272000000)
        return item

    gis = MagicMock()
    gis.content.get.side_effect = _get

    candidates = agol_sync.detect_pull_candidates(db, gis, _config_no_cache)
    assert [c.dataset_id for c in candidates] == ["ds_drift"]

    # Catalogue rows untouched.
    fresh = inventory_manager.get_dataset(db, "ds_drift")
    assert fresh["sync_status"] == "clean"
