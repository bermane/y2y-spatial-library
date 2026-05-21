"""AGOL integration: catalogue ↔ ArcGIS Online sync.

Phase A scope (this module's current state): foundation only.

This module owns the catalogue ↔ AGOL bridge described in
DESIGN.md §15:

* Authentication (named-user OAuth profile cached in
  ``~/.arcgis/profile_<name>``).
* Folder and category mapping (catalogue category → AGOL folder /
  AGOL item category).
* Item-property composition (catalogue row → AGOL item property
  dict).
* The eventual ``push()`` / ``pull()`` / ``adopt()`` / ``unpublish()``
  operations and the full ``sync_status`` state machine (Phase B+).

In Phase A the module ships:

* ``get_gis()`` to open an authenticated GIS connection.
* ``resolve_group_id()`` to look up the Conservation Atlas group on
  first contact (cached locally).
* ``ensure_org_categories()`` to create the 10-category typology in
  the AGOL org if missing.
* Pure helpers (``compute_target_folder``, ``compute_agol_category``,
  ``compute_item_properties``) for use in tests and future write
  paths.

Phase B adds ``push()`` + thumbnail generation + VTPK build.
Phase C adds ``adopt()`` + auto-sync hooks + ``reconcile_bidirectional()``.
Phase D adds ``pull()`` + conflict resolution.
Phase E adds ``unpublish()``.

The module never reaches the network at import time; an ``arcgis``
import failure surfaces only when the steward invokes an operation
that actually needs a GIS connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from . import taxonomy
from .agol_config import AgolConfig, cache_group_id


# ----------------------------------------------------------------------------
# Exception types
# ----------------------------------------------------------------------------

class AgolError(Exception):
    """Base exception for AGOL-integration failures."""


class AgolAuthError(AgolError):
    """Authentication / profile-load failure.

    Raised when ``get_gis()`` can't establish a connection — usually
    because the steward hasn't run ``y2y agol-sync login`` yet, or
    the saved profile has expired tokens.
    """


class AgolToolingError(AgolError):
    """A required local tool isn't available.

    Currently surfaces only when a ``vector-tile-layer`` push needs
    ``arcpy`` (ArcGIS Pro's bundled Python) and the current
    interpreter can't import it. See ``pipeline/agol_vtpk.py``.
    """


class AgolGroupNotFoundError(AgolError):
    """The configured Conservation Atlas group doesn't exist in the org."""


# ----------------------------------------------------------------------------
# Result types
# ----------------------------------------------------------------------------

class SyncResult(NamedTuple):
    """Outcome of a single push / pull / adopt operation."""

    dataset_id: str
    action: str               # 'push' / 'pull' / 'adopt' / 'unpublish'
    sync_status_before: str
    sync_status_after: str
    agol_item_id: str | None
    note: str
    error: str | None = None


class StatusReport(NamedTuple):
    """Result of ``y2y agol-sync status``."""

    total_active: int
    by_status: dict[str, int]
    pull_candidates: list[PullCandidate]   # populated in --deep mode only


class PullCandidate(NamedTuple):
    """A row whose AGOL ``modified`` timestamp is newer than ``last_synced_at``."""

    dataset_id: str
    agol_item_id: str
    title: str
    last_synced_at: str | None
    agol_modified_at: str


# ----------------------------------------------------------------------------
# Pure mapping helpers
# ----------------------------------------------------------------------------

def compute_target_folder(category: str, *, prefix: str) -> str:
    """Catalogue category → AGOL folder name.

    Mirrors the spatial library's folder structure (DESIGN.md §14):
    ``Y2Y_Library/Species/``, ``Y2Y_Library/Water/``, etc. Uses the
    on-disk folder name (underscored) so the AGOL tree matches what
    the steward sees in ``library/spatial/``.

    Raises ``AgolError`` if the category isn't in the typology.
    """
    if category not in taxonomy.CATEGORY_FOLDERS:
        raise AgolError(
            f"category {category!r} is not one of the {len(taxonomy.CATEGORIES)} "
            f"canonical typology categories — cannot map to an AGOL folder."
        )
    return f"{prefix}/{taxonomy.CATEGORY_FOLDERS[category]}"


def compute_agol_category(category: str) -> str:
    """Catalogue category → AGOL item category.

    Identity by design: the AGOL "Content Categories" facet should
    carry the full display name verbatim (e.g.
    ``"Jurisdictional & Political Boundaries"``), never the
    underscored folder form. See DESIGN.md §15.

    Raises ``AgolError`` if the category isn't in the typology.
    """
    if category not in taxonomy.CATEGORIES:
        raise AgolError(
            f"category {category!r} is not one of the {len(taxonomy.CATEGORIES)} "
            f"canonical typology categories — cannot map to an AGOL category."
        )
    return category


def compute_item_properties(row: dict[str, Any]) -> dict[str, Any]:
    """Catalogue row → AGOL ``Item`` property dict.

    Maps the steward-authored extrinsic-metadata fields to the AGOL
    item property names. The mapping (DESIGN.md §7 → AGOL API):

    ===========================  ================
    Catalogue field              AGOL property
    ===========================  ================
    ``title``                    ``title``
    ``summary``                  ``snippet``
    ``description``              ``description``
    ``tags`` (``;``-delimited)   ``tags`` (list)
    ``acknowledgements``         ``accessInformation``
    ``terms_of_use``             ``licenseInfo``
    ``category`` (display name)  ``categories`` (list with 1 entry)
    ===========================  ================

    Also stamps a stable ``typeKeywords`` list that lets future
    queries find Y2Y items (``Y2Y``, ``Y2Y:dataset_id:<id>``,
    ``Y2Y:category:<category>``).
    """
    tags_raw = row.get("tags") or ""
    tags = [t.strip() for t in tags_raw.split(";") if t.strip()]
    category = row.get("category")

    type_keywords = ["Y2Y"]
    if row.get("dataset_id"):
        type_keywords.append(f"Y2Y:dataset_id:{row['dataset_id']}")
    if category:
        type_keywords.append(f"Y2Y:category:{category}")

    properties: dict[str, Any] = {
        "title": row.get("title"),
        "snippet": row.get("summary"),
        "description": row.get("description"),
        "tags": tags,
        "accessInformation": row.get("acknowledgements"),
        "licenseInfo": row.get("terms_of_use"),
        "typeKeywords": type_keywords,
    }
    if category:
        properties["categories"] = [compute_agol_category(category)]

    return properties


# ----------------------------------------------------------------------------
# GIS connection
# ----------------------------------------------------------------------------

def get_gis(config: AgolConfig):
    """Open an authenticated GIS connection from the saved profile.

    Returns an ``arcgis.gis.GIS`` instance. Raises ``AgolAuthError``
    if the profile is missing or the auth handshake fails.

    Returns ``Any`` typed only because ``arcgis`` is a runtime-only
    import in this module — keeps Y2Y's static-analysis surface
    minimal even on installs that don't need AGOL.
    """
    try:
        from arcgis.gis import GIS
    except ImportError as exc:
        raise AgolAuthError(
            "The arcgis Python SDK is not installed. "
            "Add `arcgis>=2.3` to your environment "
            "(`pip install arcgis`) and retry."
        ) from exc

    try:
        gis = GIS(profile=config.profile_name)
    except Exception as exc:
        raise AgolAuthError(
            f"Could not open AGOL connection via profile "
            f"{config.profile_name!r}. Have you run `y2y agol-sync login` "
            f"yet? Underlying error: {exc}"
        ) from exc

    # The arcgis SDK is permissive: a nonexistent profile silently
    # falls through to an anonymous (unauthenticated) connection
    # instead of raising. Verify we actually got a named-user session
    # by checking `users.me` — anonymous returns None.
    me = getattr(getattr(gis, "users", None), "me", None)
    if me is None:
        raise AgolAuthError(
            f"AGOL profile {config.profile_name!r} did not authenticate "
            f"(connection fell through to anonymous). Run "
            f"`y2y agol-sync login` to (re-)create the profile."
        )
    return gis


def login_interactive(config: AgolConfig):
    """Perform the one-time OAuth-profile bootstrap.

    Walks the steward through the ArcGIS Online OAuth handshake in
    their default browser and caches the resulting credentials at
    ``~/.arcgis/profile_<profile_name>``. Subsequent ``get_gis()``
    calls then authenticate silently.

    Requires the ``client_id`` to be set on the config (typically via
    the ``Y2Y_AGOL_CLIENT_ID`` env var). Raises ``AgolAuthError`` on
    missing client_id or failed handshake.
    """
    if not config.client_id:
        raise AgolAuthError(
            "Cannot run interactive login without an OAuth client_id. "
            "Set Y2Y_AGOL_CLIENT_ID in your environment (it's the public "
            "client identifier from your Y2Y AGOL OAuth app registration), "
            "or add `client_id: ...` to ~/.y2y/agol_config.yaml."
        )

    try:
        from arcgis.gis import GIS
    except ImportError as exc:
        raise AgolAuthError(
            "The arcgis Python SDK is not installed."
        ) from exc

    try:
        # `set_profile_name` writes the credentials cache after a
        # successful OAuth dance. The steward sees a browser tab
        # open for the consent flow.
        return GIS(
            url=config.portal_url,
            client_id=config.client_id,
            profile=config.profile_name,
            set_profile_name=True,
        )
    except Exception as exc:
        raise AgolAuthError(
            f"OAuth login failed: {exc}"
        ) from exc


# ----------------------------------------------------------------------------
# Conservation Atlas group resolution + org category bootstrap
# ----------------------------------------------------------------------------

def resolve_group_id(gis, config: AgolConfig) -> str:
    """Return the Conservation Atlas group ID; cache on first lookup.

    Looks up the configured group by exact name. If the steward
    belongs to multiple orgs / multiple groups share the name, the
    first match wins (logged so the steward can disambiguate via
    config if needed).

    Cached in ``~/.y2y/agol_group_cache.json`` so subsequent runs
    skip the network call.
    """
    if config.conservation_atlas_group_id:
        return config.conservation_atlas_group_id

    name = config.conservation_atlas_group_name
    matches = gis.groups.search(query=f'title:"{name}"')
    if not matches:
        raise AgolGroupNotFoundError(
            f"AGOL group {name!r} not found in this org. "
            f"Create it on the AGOL web UI first, or override "
            f"`conservation_atlas_group_name` in ~/.y2y/agol_config.yaml."
        )

    # Prefer an exact title match; otherwise take the first result
    # and surface ambiguity in the cached note.
    exact = [g for g in matches if getattr(g, "title", None) == name]
    chosen = exact[0] if exact else matches[0]
    group_id = chosen.id

    cache_group_id(name, group_id)
    return group_id


def ensure_org_categories(gis, config: AgolConfig) -> list[str]:
    """Ensure every catalogue category exists as an AGOL Content Category.

    AGOL's "Content Categories" are an org-level facet that catalogue
    items can be tagged with. We mirror the spatial typology (10
    full-display-name categories from ``pipeline.taxonomy``) into the
    org's category schema so item ``categories`` payloads resolve at
    push time.

    Returns the list of category names this call created (empty if
    everything already existed).

    Requires the calling user to have org-admin privileges
    (creating categories is admin-only). Surfaces a clear error
    otherwise.
    """
    expected = list(taxonomy.CATEGORIES)

    # arcgis SDK exposes the category schema via the
    # ContentCategorySchemaManager on the org's Content. Older
    # SDK versions used a slightly different path; the import is
    # kept lazy to tolerate version drift without breaking the
    # rest of the module.
    try:
        schema_manager = gis.content.categories
    except AttributeError as exc:
        raise AgolError(
            "This installation of `arcgis` doesn't expose "
            "`gis.content.categories`. Upgrade arcgis to >=2.3."
        ) from exc

    try:
        existing_schema = schema_manager.schema or {}
    except Exception as exc:
        raise AgolError(
            f"Failed to fetch existing AGOL category schema: {exc}"
        ) from exc

    existing_names = _walk_category_names(existing_schema)
    missing = [c for c in expected if c not in existing_names]
    if not missing:
        return []

    # Build a flat top-level category list under the org root.
    # The SDK accepts the schema in a nested-dict shape; we
    # preserve any pre-existing categories the steward set up
    # outside our 10 (don't be destructive about other org
    # categories).
    new_schema = _merge_category_schema(existing_schema, expected)
    try:
        schema_manager.assign_to_items  # smoke-check attribute exists
        schema_manager.schema = new_schema
    except Exception as exc:
        raise AgolError(
            f"Failed to write AGOL category schema (org-admin "
            f"privileges required): {exc}"
        ) from exc

    return missing


def _walk_category_names(schema: dict[str, Any]) -> set[str]:
    """Flatten the AGOL category schema into a set of category title strings."""
    names: set[str] = set()
    for cat in schema.get("categories", []) or []:
        title = cat.get("title")
        if title:
            names.add(title)
        names.update(_walk_category_names(cat))
    return names


def _merge_category_schema(
    existing: dict[str, Any],
    desired_titles: list[str],
) -> dict[str, Any]:
    """Add any missing desired-title categories at the top level.

    Preserves any pre-existing categories already in the schema.
    Returns a new dict; doesn't mutate the input.
    """
    out = {
        "categories": list(existing.get("categories", []) or []),
    }
    existing_top_titles = {c.get("title") for c in out["categories"]}
    for title in desired_titles:
        if title not in existing_top_titles:
            out["categories"].append({"title": title})
    return out
