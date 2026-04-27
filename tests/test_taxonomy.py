"""Tests for taxonomy constants and helpers.

Categories and subcategories are stored as **display names** (full names
from the typology file). On-disk folders use the underscored
abbreviations from CATEGORY_FOLDERS / SUBCATEGORY_FOLDERS.
"""

from __future__ import annotations

from pipeline import taxonomy


# --- structure ----------------------------------------------------------

def test_categories_count_is_nine() -> None:
    assert len(taxonomy.CATEGORIES) == 9


def test_categories_use_display_names() -> None:
    assert "Administrative & Jurisdictional Boundaries" in taxonomy.CATEGORIES
    assert "Species & Species at Risk" in taxonomy.CATEGORIES
    assert "Connectivity & Wildlife Movement" in taxonomy.CATEGORIES


def test_category_folders_round_trip() -> None:
    for display, folder in taxonomy.CATEGORY_FOLDERS.items():
        assert taxonomy.FOLDER_TO_CATEGORY[folder] == display


def test_species_subcategories_use_display_names() -> None:
    assert "Species & Species at Risk" in taxonomy.SUBCATEGORIES
    subs = taxonomy.SUBCATEGORIES["Species & Species at Risk"]
    assert "Caribou" in subs
    assert "Grizzly Bear" in subs  # display, not folder
    assert "Multi-Species" in subs


def test_subcategory_folders_round_trip() -> None:
    for cat, mapping in taxonomy.SUBCATEGORY_FOLDERS.items():
        for display, folder in mapping.items():
            assert taxonomy.SUBCATEGORY_FROM_FOLDER[cat][folder] == display


def test_category_requires_subcategory_only_for_species() -> None:
    assert taxonomy.category_requires_subcategory("Species & Species at Risk")
    assert not taxonomy.category_requires_subcategory("Water")
    assert not taxonomy.category_requires_subcategory("Climate Resilience")


def test_is_valid_subcategory_for_species() -> None:
    assert taxonomy.is_valid_subcategory("Species & Species at Risk", "Caribou")
    assert taxonomy.is_valid_subcategory("Species & Species at Risk", "Grizzly Bear")
    assert not taxonomy.is_valid_subcategory("Species & Species at Risk", "Cougar")
    assert not taxonomy.is_valid_subcategory("Species & Species at Risk", None)


def test_is_valid_subcategory_for_other_categories() -> None:
    assert taxonomy.is_valid_subcategory("Water", None)
    assert taxonomy.is_valid_subcategory("Water", "")
    assert not taxonomy.is_valid_subcategory("Water", "Anything")


def test_format_labels_cover_canonical_extensions() -> None:
    assert taxonomy.FORMAT_LABELS[".gpkg"] == "GeoPackage"
    assert taxonomy.FORMAT_LABELS[".tif"] == "Cloud Optimized GeoTIFF"
    assert taxonomy.FORMAT_LABELS[".tiff"] == "Cloud Optimized GeoTIFF"


def test_ingest_statuses_exclude_tombstoned() -> None:
    assert "tombstoned" not in taxonomy.INGEST_STATUSES
    assert "active" in taxonomy.INGEST_STATUSES
    assert "deprecated" in taxonomy.INGEST_STATUSES
    assert "tombstoned" in taxonomy.ALL_STATUSES


# --- guess_category / guess_subcategory --------------------------------

def test_guess_category_admin_boundary() -> None:
    assert taxonomy.guess_category("Y2Y_RegionBoundary") == "Administrative & Jurisdictional Boundaries"
    assert taxonomy.guess_category("province_borders_2024") == "Administrative & Jurisdictional Boundaries"
    assert taxonomy.guess_category("First_Nations_Reserves") == "Administrative & Jurisdictional Boundaries"


def test_guess_category_water() -> None:
    assert taxonomy.guess_category("streams_2024") == "Water"
    assert taxonomy.guess_category("watersheds_alberta") == "Water"
    assert taxonomy.guess_category("riparian_buffers") == "Water"


def test_guess_category_species() -> None:
    assert taxonomy.guess_category("caribou_range_2023") == "Species & Species at Risk"
    assert taxonomy.guess_category("grizzly_bear_habitat") == "Species & Species at Risk"


def test_guess_category_climate() -> None:
    assert taxonomy.guess_category("climate_refugia_2050") == "Climate Resilience"


def test_guess_category_protected_areas() -> None:
    assert taxonomy.guess_category("national_parks_canada") == "Protected Areas & Conservation Lands"
    assert taxonomy.guess_category("conservation_easements") == "Protected Areas & Conservation Lands"


def test_guess_category_threats() -> None:
    assert taxonomy.guess_category("road_network_v2") == "Threats, Human Footprint & Infrastructure"
    assert taxonomy.guess_category("pipelines_2024") == "Threats, Human Footprint & Infrastructure"


def test_guess_category_returns_none_when_no_keywords_match() -> None:
    assert taxonomy.guess_category("dataset_v1") is None
    assert taxonomy.guess_category("y2y_misc_layer") is None


def test_guess_category_does_not_match_substring_within_word() -> None:
    """'carbon' should not match 'carbonate' (substring) — word boundaries matter."""
    assert taxonomy.guess_category("calcium_carbonate") is None


def test_guess_subcategory_only_for_species() -> None:
    species = "Species & Species at Risk"
    assert taxonomy.guess_subcategory(species, "caribou_range") == "Caribou"
    assert taxonomy.guess_subcategory(species, "grizzly_bear_dens") == "Grizzly Bear"
    assert taxonomy.guess_subcategory(species, "wolverine_dens") == "Wolverine"
    # Non-Species categories return None regardless of text
    assert taxonomy.guess_subcategory("Water", "caribou_range") is None
    assert taxonomy.guess_subcategory(None, "caribou_range") is None


def test_guess_subcategory_returns_none_when_no_match() -> None:
    assert taxonomy.guess_subcategory("Species & Species at Risk", "habitat_mapping_v1") is None
