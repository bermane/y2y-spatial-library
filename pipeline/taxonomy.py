"""Taxonomy constants for the Y2Y spatial library.

Folder names match what was scaffolded under ``library/`` and what the
typology spreadsheet (Spatial_Data_Typology.xlsx) defines. Keeping
them in code lets validators check incoming category/subcategory
overrides without re-scanning the filesystem.
"""

from __future__ import annotations

# Top-level categories — display names (what the steward sees in
# pending.xlsx and the generated inventory.xlsx). Display names match
# the typology file (Spatial_Data_Typology.xlsx) verbatim, including
# ampersands and spaces. Order follows the typology document.
#
# Schema CHECK constraint (pipeline/schema.sql) hard-codes this same
# list — keep the two in sync. Migration 006 carried the 9→10 legacy
# transition; migration 010 carries the 2026 mid-year revision
# (merge the two boundary/tenure categories, rename Climate
# Resilience→Climate Change and Human Dimensions→Human Dimensions of
# Conservation, split out Demographics & Socioeconomic Data).
CATEGORIES: tuple[str, ...] = (
    "Boundaries, Tenure & Governance",
    "Biodiversity & Ecosystems",
    "Climate Change",
    "Connectivity & Wildlife Movement",
    "Species",
    "Water",
    "Land Cover, Land Use & Disturbance",
    "Human Dimensions of Conservation",
    "Threats & Infrastructure",
    "Demographics & Socioeconomic Data",
)

# Display name → on-disk folder name. The folder names follow the
# Title_Case_Underscores convention from DESIGN.md §6 and match the
# subdirectories scaffolded under library/spatial/.
CATEGORY_FOLDERS: dict[str, str] = {
    "Boundaries, Tenure & Governance": "Boundaries_Tenure_Governance",
    "Biodiversity & Ecosystems": "Biodiversity_Ecosystems",
    "Climate Change": "Climate_Change",
    "Connectivity & Wildlife Movement": "Connectivity_Wildlife_Movement",
    "Species": "Species",
    "Water": "Water",
    "Land Cover, Land Use & Disturbance": "Land_Cover_Use_Disturbance",
    "Human Dimensions of Conservation": "Human_Dimensions_Conservation",
    "Threats & Infrastructure": "Threats_Infrastructure",
    "Demographics & Socioeconomic Data": "Demographics_Socioeconomic",
}

# Reverse mapping for parsing library paths back to display names
# (used by lifecycle.rename when re-deriving category/subcategory from a
# new path, and by reconcile when reporting findings).
FOLDER_TO_CATEGORY: dict[str, str] = {v: k for k, v in CATEGORY_FOLDERS.items()}

# Sub-categories per (display-name) category. Display name → tuple of
# display sub-names. A category absent from this map admits no sub.
SUBCATEGORIES: dict[str, tuple[str, ...]] = {
    "Species": (
        "Caribou",
        "Elk",
        "Goat",
        "Grizzly Bear",
        "Multi-Species",
        "Wolverine",
        # Catch-all for species that don't fit the named taxa above
        # (e.g., fish, birds, single-species datasets outside the
        # core ungulate/bear/wolverine set).
        "Other",
    ),
}

# Sub-category display name → on-disk folder name, per category.
SUBCATEGORY_FOLDERS: dict[str, dict[str, str]] = {
    "Species": {
        "Caribou": "Caribou",
        "Elk": "Elk",
        "Goat": "Goat",
        "Grizzly Bear": "Grizzly_Bear",
        "Multi-Species": "Multi_Species",
        "Wolverine": "Wolverine",
        "Other": "Other",
    },
}

# Reverse: per category, folder → display sub-name.
SUBCATEGORY_FROM_FOLDER: dict[str, dict[str, str]] = {
    cat: {v: k for k, v in m.items()}
    for cat, m in SUBCATEGORY_FOLDERS.items()
}

# Mapping from file extension to the canonical inventory ``format`` label.
FORMAT_LABELS: dict[str, str] = {
    ".gpkg": "GeoPackage",
    ".tif": "Cloud Optimized GeoTIFF",
    ".tiff": "Cloud Optimized GeoTIFF",
}

# Permitted ``status`` values. ``tombstoned`` is set by removal logic in
# later sessions, never at ingest.
INGEST_STATUSES: tuple[str, ...] = ("active", "deprecated")
ALL_STATUSES: tuple[str, ...] = ("active", "deprecated", "tombstoned")

# Dataset classification. Auto-set to ``vector`` for vector targets at
# scan; for raster targets the steward declares ``continuous`` or
# ``categorical`` (which drives resampling, TIFF predictor, and default
# NoData — see DESIGN.md §11). The ``vector`` label exists so the
# inventory column is never empty for an admitted dataset.
CLASSIFICATIONS: tuple[str, ...] = ("vector", "continuous", "categorical")
RASTER_CLASSIFICATIONS: tuple[str, ...] = ("continuous", "categorical")
VECTOR_CLASSIFICATION: str = "vector"


def category_requires_subcategory(category: str) -> bool:
    """True if the category has any defined sub-categories."""
    return category in SUBCATEGORIES


def is_valid_subcategory(category: str, subcategory: str | None) -> bool:
    """Validate the (category, subcategory) pair.

    - If the category has sub-categories defined, ``subcategory`` must be
      one of them.
    - If the category has no sub-categories, ``subcategory`` must be
      empty/None.
    """
    allowed = SUBCATEGORIES.get(category)
    if allowed is None:
        return not subcategory
    return subcategory in allowed


# --- Auto-categorisation from text (filename, title, ...) -------------
#
# The steward can override at any time; these keywords just provide a
# best-guess starting point so an obvious dataset doesn't waste a column
# the steward has to fill manually. As the typology evolves, edit this
# table — it's the single source of truth for keyword → category mapping.

# Each entry is either a keyword string (default weight 1) or a
# (keyword, weight) tuple for high-signal terms that should outweigh
# generic ties. E.g. "ipca" / "wma" / "iucn" are unambiguously
# Protected Areas territory; if they co-occur with a generic word like
# "boundary", they should win.
KeywordEntry = str | tuple[str, int]

CATEGORY_KEYWORDS: dict[str, tuple[KeywordEntry, ...]] = {
    "Boundaries, Tenure & Governance": (
        # Boundaries (formerly "Jurisdictional & Political Boundaries")
        "boundary", "boundaries", "border", "borders",
        "province", "provincial", "state", "states",
        "jurisdiction", "jurisdictional", "admin", "administrative",
        "region", "regional",
        "municipal", "municipality",
        "census", "constituency", "constituencies",
        "international", "political",
        ("population_center", 2), ("population_centers", 2),
        ("management_unit", 2), ("management_units", 2),
        # Designations / tenure (formerly "Land Designations & Tenure")
        "park", "parks", "wilderness",
        ("wma", 2), ("ipca", 2), ("iucn", 2),
        "conservation", "easement", "easements",
        "protected",
        "designation", "designations",
        "tenure", "tenures", "stewardship",
        "first_nations", "treaty", "treaties",
    ),
    "Biodiversity & Ecosystems": (
        "ecoregion", "ecoregions", "ecosystem", "ecosystems",
        "biodiversity", "biophysical", "biome", "biomes",
        "kba", "ecological", "benchmark", "benchmarks",
        # Typology lists DEM/LiDAR/terrain products under this category.
        "elevation", "slope", "aspect", "hillshade",
        "lidar", "dem",
    ),
    "Climate Change": (
        "climate", "refugia", "refugium",
        "carbon", "biomass",
        "fire", "wildfire", "burn_regime", "disturbance_regime",
        "velocity", "resilient", "resilience",
        "temperature", "precipitation", "projection", "projections",
        "normals",
    ),
    "Connectivity & Wildlife Movement": (
        "corridor", "corridors", "linkage", "linkages",
        "connectivity", "permeability", "resistance",
        "telemetry", "movement", "movements", "barrier", "barriers",
    ),
    "Species": (
        "species", "habitat", "wildlife",
        "caribou", "elk", "goat", "grizzly", "bear",
        "wolverine", "wolf",
        "salmon", "trout", "fish",
        "ungulate", "ungulates",
        "distribution", "distributions",
    ),
    "Water": (
        "water", "watershed", "watersheds",
        "hydrology", "hydrologic", "hydrography", "hydro",
        "stream", "streams", "river", "rivers", "lake", "lakes",
        "riparian", "aquatic",
    ),
    "Land Cover, Land Use & Disturbance": (
        "landcover", "land_cover", "land_use",
        "ndvi", "phenology",
        "burn_severity", "burn", "burns",
        "insect", "disease",
        "vegetation", "forest", "grassland",
        "change_detection",
    ),
    "Human Dimensions of Conservation": (
        # Social-science / governance-research side. Population
        # demographics + socioeconomic data now live in their own
        # category (below), so those keywords are NOT here.
        "governance",
        "community", "communities",
        "stakeholder", "stakeholders",
        # "indigenous" lives here (per typology: "Indigenous-led
        # research"); the related-but-distinct "first_nations" sits
        # under Boundaries, Tenure & Governance (typology: "First
        # Nations lands").
        "indigenous",
        "attitudes", "perception", "perceptions",
        "survey", "surveys",
    ),
    "Threats & Infrastructure": (
        "road", "roads", "railway", "railways", "rail",
        "pipeline", "pipelines", "utility", "utilities",
        "extraction", "development",
        "footprint", "footprints", "infrastructure", "infras",
        "cumulative", "cumulative_effects",
        "trail", "trails",
    ),
    "Demographics & Socioeconomic Data": (
        "demographic", "demographics",
        "socioeconomic", "socio_economic",
        "population",
    ),
}

# Subcategory keywords keyed by display sub-name (under "Species").
SPECIES_SUBCATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Caribou": ("caribou",),
    "Elk": ("elk",),
    "Goat": ("goat", "goats", "mountain_goat"),
    "Grizzly Bear": ("grizzly", "grizzlies", "grizzly_bear", "gb"),
    "Multi-Species": ("multi_species", "multispecies"),
    "Wolverine": ("wolverine", "wolverines"),
}


def _matches_word(slug: str, keyword: str) -> bool:
    """True iff ``keyword`` appears as a whole token in underscore-slug ``slug``.

    Prevents "carbon" from matching "carbonate" while still allowing
    multi-word keywords like ``first_nations`` to match across underscores.
    """
    return f"_{keyword}_" in f"_{slug}_"


def _keyword_and_weight(entry: KeywordEntry) -> tuple[str, int]:
    """Normalize a CATEGORY_KEYWORDS entry to (keyword, weight)."""
    if isinstance(entry, str):
        return entry, 1
    return entry[0], entry[1]


def guess_category(text: str) -> str | None:
    """Best-guess top-level category from arbitrary text. None if no match."""
    from . import utils  # local import — avoid circular at module load
    slug = utils.slugify_title(text)
    best_cat: str | None = None
    best_score = 0
    for cat, keywords in CATEGORY_KEYWORDS.items():
        score = 0
        for entry in keywords:
            kw, weight = _keyword_and_weight(entry)
            if _matches_word(slug, kw):
                score += weight
        if score > best_score:
            best_score = score
            best_cat = cat
    return best_cat


def guess_subcategory(category: str | None, text: str) -> str | None:
    """Best-guess subcategory. Only Species has subcategories in Phase A."""
    if category != "Species":
        return None
    from . import utils
    slug = utils.slugify_title(text)
    for sub, keywords in SPECIES_SUBCATEGORY_KEYWORDS.items():
        if any(_matches_word(slug, kw) for kw in keywords):
            return sub
    return None
