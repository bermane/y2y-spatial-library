"""Taxonomy constants for the Y2Y spatial library.

Folder names match what was scaffolded under ``library/`` and what the
typology spreadsheet (Spatial_Data_Typology.xlsx) defines. Keeping
them in code lets validators check incoming category/subcategory
overrides without re-scanning the filesystem.
"""

from __future__ import annotations

# Top-level categories — display names (what the steward sees in
# pending.xlsx and inventory.xlsx). Display names match the typology
# file (Spatial_Data_Typology.xlsx) verbatim, including ampersands and
# spaces. The on-disk folder names are stored separately in
# CATEGORY_FOLDERS below; the pipeline maps display↔folder when
# building library paths and parsing paths back into display names.
CATEGORIES: tuple[str, ...] = (
    "Administrative & Jurisdictional Boundaries",
    "Biodiversity & Ecosystems",
    "Climate Resilience",
    "Connectivity & Wildlife Movement",
    "Species & Species at Risk",
    "Water",
    "Land Cover, Land Use & Disturbance",
    "Protected Areas & Conservation Lands",
    "Threats, Human Footprint & Infrastructure",
)

# Display name → on-disk folder name. The folder names follow the
# Title_Case_Underscores convention from DESIGN.md §6 and match the
# subdirectories scaffolded under library/.
CATEGORY_FOLDERS: dict[str, str] = {
    "Administrative & Jurisdictional Boundaries": "Admin_Juris_Boundaries",
    "Biodiversity & Ecosystems": "Biodiversity_Ecosystems",
    "Climate Resilience": "Climate_Resilience",
    "Connectivity & Wildlife Movement": "Connectivity_Wildlife_Movement",
    "Species & Species at Risk": "Species",
    "Water": "Water",
    "Land Cover, Land Use & Disturbance": "Land_Cover_Use_Disturbance",
    "Protected Areas & Conservation Lands": "Prot_Areas_Cons_Lands",
    "Threats, Human Footprint & Infrastructure": "Threats_Human_Footprint_Infras",
}

# Reverse mapping for parsing library paths back to display names
# (used by lifecycle.rename when re-deriving category/subcategory from a
# new path, and by reconcile when reporting findings).
FOLDER_TO_CATEGORY: dict[str, str] = {v: k for k, v in CATEGORY_FOLDERS.items()}

# Sub-categories per (display-name) category. Display name → tuple of
# display sub-names. A category absent from this map admits no sub.
SUBCATEGORIES: dict[str, tuple[str, ...]] = {
    "Species & Species at Risk": (
        "Caribou",
        "Elk",
        "Goat",
        "Grizzly Bear",
        "Multi-Species",
        "Wolverine",
    ),
}

# Sub-category display name → on-disk folder name, per category.
SUBCATEGORY_FOLDERS: dict[str, dict[str, str]] = {
    "Species & Species at Risk": {
        "Caribou": "Caribou",
        "Elk": "Elk",
        "Goat": "Goat",
        "Grizzly Bear": "Grizzly_Bear",
        "Multi-Species": "Multi_Species",
        "Wolverine": "Wolverine",
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

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Administrative & Jurisdictional Boundaries": (
        "boundary", "boundaries", "border", "borders",
        "province", "provincial", "state", "states",
        "jurisdiction", "jurisdictional", "admin", "administrative",
        "region", "regional",
        "first_nations", "treaty", "treaties", "indigenous",
        "municipal", "municipality", "population", "census",
        "international",
    ),
    "Biodiversity & Ecosystems": (
        "ecoregion", "ecoregions", "ecosystem", "ecosystems",
        "biodiversity", "biophysical", "biome", "biomes",
        "kba", "ecological",
    ),
    "Climate Resilience": (
        "climate", "refugia", "refugium",
        "carbon", "biomass",
        "fire", "wildfire", "burn_regime", "disturbance_regime",
        "temperature", "precipitation",
    ),
    "Connectivity & Wildlife Movement": (
        "corridor", "corridors", "linkage", "linkages",
        "connectivity", "permeability", "resistance",
        "telemetry", "movement", "movements", "barrier", "barriers",
    ),
    "Species & Species at Risk": (
        "species", "habitat", "wildlife",
        "caribou", "elk", "goat", "grizzly", "bear",
        "wolverine", "wolf",
        "salmon", "trout", "fish",
        "ungulate", "ungulates",
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
    ),
    "Protected Areas & Conservation Lands": (
        "park", "parks", "wilderness", "wma", "ipca",
        "conservation", "easement", "easements",
        "protected", "iucn",
    ),
    "Threats, Human Footprint & Infrastructure": (
        "road", "roads", "railway", "railways", "rail",
        "pipeline", "pipelines", "utility", "utilities",
        "extraction", "development",
        "footprint", "footprints", "infrastructure", "infras",
        "demographic", "demographics",
    ),
}

# Subcategory keywords keyed by display sub-name (under "Species & Species at Risk").
SPECIES_SUBCATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Caribou": ("caribou",),
    "Elk": ("elk",),
    "Goat": ("goat", "goats", "mountain_goat"),
    "Grizzly Bear": ("grizzly", "grizzlies", "grizzly_bear"),
    "Multi-Species": ("multi_species", "multispecies"),
    "Wolverine": ("wolverine", "wolverines"),
}


def _matches_word(slug: str, keyword: str) -> bool:
    """True iff ``keyword`` appears as a whole token in underscore-slug ``slug``.

    Prevents "carbon" from matching "carbonate" while still allowing
    multi-word keywords like ``first_nations`` to match across underscores.
    """
    return f"_{keyword}_" in f"_{slug}_"


def guess_category(text: str) -> str | None:
    """Best-guess top-level category from arbitrary text. None if no match."""
    from . import utils  # local import — avoid circular at module load
    slug = utils.slugify_title(text)
    best_cat: str | None = None
    best_score = 0
    for cat, keywords in CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in keywords if _matches_word(slug, kw))
        if score > best_score:
            best_score = score
            best_cat = cat
    return best_cat


def guess_subcategory(category: str | None, text: str) -> str | None:
    """Best-guess subcategory. Only Species has subcategories in Phase A."""
    if category != "Species & Species at Risk":
        return None
    from . import utils
    slug = utils.slugify_title(text)
    for sub, keywords in SPECIES_SUBCATEGORY_KEYWORDS.items():
        if any(_matches_word(slug, kw) for kw in keywords):
            return sub
    return None
