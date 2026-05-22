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
