"""Tests for taxonomy constants and helpers.

Categories and subcategories are stored as **display names** (full names
from the typology file). On-disk folders use the underscored
abbreviations from CATEGORY_FOLDERS / SUBCATEGORY_FOLDERS.
"""

from __future__ import annotations

from pipeline import taxonomy


# --- structure ----------------------------------------------------------

def test_categories_count_is_ten() -> None:
    """Post-2026-workshop typology has 10 categories (added Human Dimensions
    and Land Designations & Tenure; removed Protected Areas & Conservation
    Lands which folded into the latter)."""
    assert len(taxonomy.CATEGORIES) == 10


def test_categories_use_display_names() -> None:
    assert "Jurisdictional & Political Boundaries" in taxonomy.CATEGORIES
    assert "Species" in taxonomy.CATEGORIES
    assert "Connectivity & Wildlife Movement" in taxonomy.CATEGORIES


def test_category_folders_round_trip() -> None:
    for display, folder in taxonomy.CATEGORY_FOLDERS.items():
        assert taxonomy.FOLDER_TO_CATEGORY[folder] == display


def test_species_subcategories_use_display_names() -> None:
    assert "Species" in taxonomy.SUBCATEGORIES
    subs = taxonomy.SUBCATEGORIES["Species"]
    assert "Caribou" in subs
    assert "Grizzly Bear" in subs  # display, not folder
    assert "Multi-Species" in subs
    assert "Other" in subs  # catch-all for non-listed species


def test_subcategory_folders_round_trip() -> None:
    for cat, mapping in taxonomy.SUBCATEGORY_FOLDERS.items():
        for display, folder in mapping.items():
            assert taxonomy.SUBCATEGORY_FROM_FOLDER[cat][folder] == display


def test_category_requires_subcategory_only_for_species() -> None:
    assert taxonomy.category_requires_subcategory("Species")
    assert not taxonomy.category_requires_subcategory("Water")
    assert not taxonomy.category_requires_subcategory("Climate Resilience")


def test_is_valid_subcategory_for_species() -> None:
    assert taxonomy.is_valid_subcategory("Species", "Caribou")
    assert taxonomy.is_valid_subcategory("Species", "Grizzly Bear")
    assert not taxonomy.is_valid_subcategory("Species", "Cougar")
    assert not taxonomy.is_valid_subcategory("Species", None)


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

def test_guess_category_jurisdictional_boundaries() -> None:
    assert taxonomy.guess_category("Y2Y_RegionBoundary") == "Jurisdictional & Political Boundaries"
    assert taxonomy.guess_category("province_borders_2024") == "Jurisdictional & Political Boundaries"
    assert taxonomy.guess_category("municipal_census_2021") == "Jurisdictional & Political Boundaries"


def test_guess_category_first_nations_lands_is_tenure_not_boundary() -> None:
    """Post-2026 typology: First Nations lands are land-tenure, not
    political boundary. The keyword ``first_nations`` now resolves to
    Land Designations & Tenure (was Administrative & Jurisdictional
    Boundaries pre-revision)."""
    assert taxonomy.guess_category("First_Nations_Reserves") == "Land Designations & Tenure"


def test_guess_category_water() -> None:
    assert taxonomy.guess_category("streams_2024") == "Water"
    assert taxonomy.guess_category("watersheds_alberta") == "Water"
    assert taxonomy.guess_category("riparian_buffers") == "Water"


def test_guess_category_species() -> None:
    assert taxonomy.guess_category("caribou_range_2023") == "Species"
    assert taxonomy.guess_category("grizzly_bear_habitat") == "Species"


def test_guess_category_climate() -> None:
    assert taxonomy.guess_category("climate_refugia_2050") == "Climate Resilience"


def test_guess_category_protected_areas() -> None:
    assert taxonomy.guess_category("national_parks_canada") == "Land Designations & Tenure"
    assert taxonomy.guess_category("conservation_easements") == "Land Designations & Tenure"


def test_guess_category_threats() -> None:
    assert taxonomy.guess_category("road_network_v2") == "Threats & Infrastructure"
    assert taxonomy.guess_category("pipelines_2024") == "Threats & Infrastructure"


def test_guess_category_returns_none_when_no_keywords_match() -> None:
    assert taxonomy.guess_category("dataset_v1") is None
    assert taxonomy.guess_category("y2y_misc_layer") is None


def test_guess_category_does_not_match_substring_within_word() -> None:
    """'carbon' should not match 'carbonate' (substring) — word boundaries matter."""
    assert taxonomy.guess_category("calcium_carbonate") is None


def test_guess_subcategory_only_for_species() -> None:
    species = "Species"
    assert taxonomy.guess_subcategory(species, "caribou_range") == "Caribou"
    assert taxonomy.guess_subcategory(species, "grizzly_bear_dens") == "Grizzly Bear"
    assert taxonomy.guess_subcategory(species, "wolverine_dens") == "Wolverine"
    # Non-Species categories return None regardless of text
    assert taxonomy.guess_subcategory("Water", "caribou_range") is None
    assert taxonomy.guess_subcategory(None, "caribou_range") is None


def test_guess_subcategory_returns_none_when_no_match() -> None:
    assert taxonomy.guess_subcategory("Species", "habitat_mapping_v1") is None


# --- weighted keywords -------------------------------------------------

def test_ipca_outweighs_boundary_for_protected_areas() -> None:
    """Tied keyword counts: 'ipca' (high-signal, weight 2) beats 'boundary' (weight 1)."""
    assert taxonomy.guess_category("ross_river_ipca_boundary") == "Land Designations & Tenure"


def test_wma_outweighs_boundary() -> None:
    assert taxonomy.guess_category("highwood_wma_boundary") == "Land Designations & Tenure"


def test_iucn_outweighs_boundary() -> None:
    assert taxonomy.guess_category("iucn_areas_boundary") == "Land Designations & Tenure"


def test_gb_keyword_resolves_to_grizzly_bear_subcategory() -> None:
    species = "Species"
    assert taxonomy.guess_subcategory(species, "gb_habitat_female_fall") == "Grizzly Bear"
    assert taxonomy.guess_subcategory(species, "GBHabitat_Female_Fall") == "Grizzly Bear"
