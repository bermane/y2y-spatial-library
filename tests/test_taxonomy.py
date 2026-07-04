"""Tests for taxonomy constants and helpers.

Categories and subcategories are stored as **display names** (full names
from the typology file). On-disk folders use the underscored
abbreviations from CATEGORY_FOLDERS / SUBCATEGORY_FOLDERS.
"""

from __future__ import annotations

from pipeline import taxonomy


# --- structure ----------------------------------------------------------

def test_categories_count_is_ten() -> None:
    """The 2026 mid-year revision keeps 10 categories: −1 from merging the
    two boundary/tenure categories into 'Boundaries, Tenure & Governance',
    +1 from adding 'Demographics & Socioeconomic Data'."""
    assert len(taxonomy.CATEGORIES) == 10


def test_categories_use_display_names() -> None:
    assert "Boundaries, Tenure & Governance" in taxonomy.CATEGORIES
    assert "Species" in taxonomy.CATEGORIES
    assert "Connectivity & Wildlife Movement" in taxonomy.CATEGORIES
    assert "Demographics & Socioeconomic Data" in taxonomy.CATEGORIES


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
    assert not taxonomy.category_requires_subcategory("Climate Change")


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

def test_guess_category_boundaries_tenure_governance() -> None:
    """The 2026 mid-year revision merged the old boundary + tenure
    categories into one, so boundaries, First Nations lands, parks, and
    tenure keywords all resolve to 'Boundaries, Tenure & Governance'."""
    btg = "Boundaries, Tenure & Governance"
    assert taxonomy.guess_category("Y2Y_RegionBoundary") == btg
    assert taxonomy.guess_category("province_borders_2024") == btg
    assert taxonomy.guess_category("municipal_census_2021") == btg
    assert taxonomy.guess_category("First_Nations_Reserves") == btg


def test_guess_category_water() -> None:
    assert taxonomy.guess_category("streams_2024") == "Water"
    assert taxonomy.guess_category("watersheds_alberta") == "Water"
    assert taxonomy.guess_category("riparian_buffers") == "Water"


def test_guess_category_species() -> None:
    assert taxonomy.guess_category("caribou_range_2023") == "Species"
    assert taxonomy.guess_category("grizzly_bear_habitat") == "Species"


def test_guess_category_climate() -> None:
    assert taxonomy.guess_category("climate_refugia_2050") == "Climate Change"


def test_guess_category_protected_areas() -> None:
    btg = "Boundaries, Tenure & Governance"
    assert taxonomy.guess_category("national_parks_canada") == btg
    assert taxonomy.guess_category("conservation_easements") == btg


def test_guess_category_demographics() -> None:
    demo = "Demographics & Socioeconomic Data"
    assert taxonomy.guess_category("population_demographics_2021") == demo
    assert taxonomy.guess_category("socioeconomic_indices") == demo


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

def test_tenure_keywords_resolve_to_boundaries_tenure_governance() -> None:
    """The 2026 merge folds boundaries and land-designation/tenure into one
    category, so high-signal tenure terms (ipca/wma/iucn) that co-occur with
    'boundary' all resolve to 'Boundaries, Tenure & Governance' — no longer a
    cross-category tiebreak, just the same category."""
    btg = "Boundaries, Tenure & Governance"
    assert taxonomy.guess_category("ross_river_ipca_boundary") == btg
    assert taxonomy.guess_category("highwood_wma_boundary") == btg
    assert taxonomy.guess_category("iucn_areas_boundary") == btg


def test_gb_keyword_resolves_to_grizzly_bear_subcategory() -> None:
    species = "Species"
    assert taxonomy.guess_subcategory(species, "gb_habitat_female_fall") == "Grizzly Bear"
    assert taxonomy.guess_subcategory(species, "GBHabitat_Female_Fall") == "Grizzly Bear"
