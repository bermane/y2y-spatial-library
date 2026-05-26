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

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from . import inventory_manager, taxonomy
from .agol_config import AgolConfig, cache_group_id
from .utils import utc_now_iso


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

    Reserved for future use. Rev 3 (2026-05-27) retired the arcpy
    auto-build path that previously raised this — the new manual-
    VTPK + ingest workflow runs without arcpy. Kept in the
    exception hierarchy in case a future capability (e.g., direct
    AGOL REST-call helpers requiring extra deps) needs a similar
    "local environment isn't ready" signal.
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

def compute_target_folder(category: str) -> str:
    """Catalogue category → AGOL folder name.

    Returns the underscored category folder name (e.g. ``"Species"``,
    ``"Water"``, ``"Juris_Political_Boundaries"``) — matching the
    on-disk folder names under ``library/spatial/``.

    AGOL folders are a **flat namespace** — no nesting, no slashes in
    folder names. An earlier version of this function returned
    ``"Y2Y_Library/<category>"`` to imply a namespace, but AGOL
    treats the slash as a literal character: ``content.add()`` accepts
    it but stores the item under just the trailing segment, while
    ``Item.move()`` rejects the slash-named target so feature layers
    silently end up stranded in My Content root. Stripping the prefix
    makes both calls consistent.

    Raises ``AgolError`` if the category isn't in the typology.
    """
    if category not in taxonomy.CATEGORY_FOLDERS:
        raise AgolError(
            f"category {category!r} is not one of the {len(taxonomy.CATEGORIES)} "
            f"canonical typology categories — cannot map to an AGOL folder."
        )
    return taxonomy.CATEGORY_FOLDERS[category]


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


def compute_service_name(row: dict[str, Any]) -> str:
    """Catalogue row → AGOL-safe service name.

    AGOL service names allow only ``[A-Za-z0-9_]`` — no spaces, no
    punctuation. This function sanitises the steward-authored
    ``title`` into a name AGOL will accept while keeping it
    human-readable:

    * Spaces → underscores.
    * Anything outside ``[A-Za-z0-9_]`` is dropped.
    * Consecutive underscores collapse to one.
    * Leading/trailing underscores are stripped.
    * Empty result (e.g., title was all special chars) falls back to
      ``dataset_id``, which is ULID-shaped and AGOL-safe by
      construction.

    Examples:

        ``"Y2Y Land Cover (2020)"``  →  ``"Y2Y_Land_Cover_2020"``
        ``"Biomass Carbon Density 2022 (t/ha)"``  →  ``"Biomass_Carbon_Density_2022_t_ha"``
        ``"GB Habitat — Female Fall"``  →  ``"GB_Habitat_Female_Fall"``

    Notes:

    * The service name is what appears in the published service's
      URL (e.g. ``https://services.../rest/services/<name>/ImageServer``).
      The human-readable title is preserved separately via
      ``item.update(item_properties={'title': ...})`` after publish.
    * AGOL requires uniqueness per user/org. Two datasets whose
      titles collapse to the same sanitised form would conflict —
      ``publish_hosted_imagery_layer`` raises a clear "service by
      that name already exists" error in that case.
    """
    import re
    title = (row.get("title") or "").strip()
    # Replace any non-alphanumeric character (incl. spaces, parens,
    # slashes, em-dashes, accented letters, etc.) with an underscore.
    # This keeps readable separators where punctuation existed
    # ("t/ha" → "t_ha") rather than dropping the chars and gluing
    # neighbouring tokens together ("t/ha" → "tha").
    candidate = re.sub(r"[^A-Za-z0-9]", "_", title)
    # Collapse runs of underscores.
    candidate = re.sub(r"_+", "_", candidate)
    # Strip leading/trailing underscores.
    candidate = candidate.strip("_")
    return candidate or (row.get("dataset_id") or "")


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

    # AGOL categories: parent + (optional) subcategory. The subcategory
    # is identity-mapped to its AGOL counterpart since the schema we
    # write to the org mirrors the catalogue subcategory display names
    # exactly. Both go in `categories` as a flat list — AGOL's tree
    # resolves them by title.
    if category:
        cats = [compute_agol_category(category)]
        sub = row.get("subcategory")
        if sub:
            cats.append(sub)
        properties["categories"] = cats

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


class CategorySchemaDiff(NamedTuple):
    """Report from :func:`ensure_org_categories`."""

    will_add: list[str]
    will_orphan: list[str]
    unchanged: list[str]
    applied: bool


def build_canonical_schema() -> list[dict[str, Any]]:
    """Construct the canonical AGOL category schema from the catalogue typology.

    Single source of truth: ``pipeline.taxonomy.CATEGORIES`` (ordered)
    + ``SUBCATEGORIES`` (Species → 7 subs). Top-level categories in
    typology document order. Species gets its 7 subcategories nested
    using the catalogue's display-name casing (``Multi-Species``,
    not ``Multi-species``).

    Returns the AGOL-expected ``[{"title": "Categories", "categories":
    [...]}]`` shape.
    """
    children: list[dict[str, Any]] = []
    for cat in taxonomy.CATEGORIES:
        subs = taxonomy.SUBCATEGORIES.get(cat, ())
        children.append({
            "title": cat,
            "categories": [{"title": s, "categories": []} for s in subs],
        })
    return [{"title": "Categories", "categories": children}]


def ensure_org_categories(
    gis,
    config: AgolConfig,
    *,
    apply: bool = True,
) -> CategorySchemaDiff:
    """Bring the AGOL org's category schema into line with the catalogue typology.

    Reads the current AGOL Content Category schema, computes the diff
    against ``build_canonical_schema()``, and (if ``apply=True``)
    writes the canonical schema, replacing whatever was there.

    **This is a destructive write.** Any category in the org's schema
    that isn't in the catalogue typology is orphaned — items tagged
    with those categories lose their tags. The 2026 typology revision
    (see DESIGN.md §7 + migration 006) made this kind of rewrite
    inevitable; the steward consented in the AGOL-integration plan
    (.claude/plans/parallel-toasting-storm.md) to bringing AGOL into
    alignment.

    With ``apply=False``, returns the diff without writing — useful
    for a dry-run preview.

    Requires org-admin privileges on AGOL. Raises ``AgolError`` if
    the calling user can't write the schema.
    """
    try:
        schema_manager = gis.content.categories
    except AttributeError as exc:
        raise AgolError(
            "This installation of `arcgis` doesn't expose "
            "`gis.content.categories`. Upgrade arcgis to >=2.3."
        ) from exc

    try:
        existing_schema = schema_manager.schema or []
    except Exception as exc:
        raise AgolError(
            f"Failed to fetch existing AGOL category schema: {exc}"
        ) from exc

    canonical_schema = build_canonical_schema()

    existing_top = _top_level_titles(existing_schema)
    canonical_top = _top_level_titles(canonical_schema)

    will_add = sorted(canonical_top - existing_top)
    will_orphan = sorted(existing_top - canonical_top)
    unchanged = sorted(canonical_top & existing_top)

    if not apply:
        return CategorySchemaDiff(
            will_add=will_add,
            will_orphan=will_orphan,
            unchanged=unchanged,
            applied=False,
        )

    # No-op if everything matches at both the top level and the full
    # nested tree (catches the case where someone re-runs after a
    # successful apply).
    if (
        not will_add
        and not will_orphan
        and _walk_category_names(existing_schema)
        == _walk_category_names(canonical_schema)
    ):
        return CategorySchemaDiff(
            will_add=[],
            will_orphan=[],
            unchanged=sorted(canonical_top),
            applied=False,
        )

    try:
        schema_manager.schema = canonical_schema
    except Exception as exc:
        raise AgolError(
            f"Failed to write AGOL category schema (org-admin "
            f"privileges required): {exc}"
        ) from exc

    return CategorySchemaDiff(
        will_add=will_add,
        will_orphan=will_orphan,
        unchanged=unchanged,
        applied=True,
    )


def _walk_category_names(schema: Any) -> set[str]:
    """Flatten the AGOL category schema into a set of every title string.

    The AGOL Content-Category schema is a tree: a top-level list of
    nodes (typically one root node with title 'Categories'), each
    node a dict ``{"title": str, "categories": [...nested nodes...]}``.
    This helper recurses through any depth and collects every
    ``title``.

    Accepts dicts (single node) or lists (collection of nodes) at any
    level — older SDK versions return either shape depending on org
    config, so we're defensive about both.
    """
    names: set[str] = set()

    if isinstance(schema, list):
        for node in schema:
            names.update(_walk_category_names(node))
        return names

    if isinstance(schema, dict):
        title = schema.get("title")
        if title:
            names.add(title)
        children = schema.get("categories")
        if children:
            names.update(_walk_category_names(children))
        return names

    return names


def _top_level_titles(schema: Any) -> set[str]:
    """Titles of the immediate children of the schema's root.

    The 'root' is the wrapper node titled 'Categories' (singular) —
    every direct child of that root is a top-level category in the
    org's facet tree. If the schema has no such wrapper, the
    schema-itself's list members are treated as top-level.
    """
    if isinstance(schema, list):
        # Common shape: [{"title": "Categories", "categories": [...]}]
        # Skip the wrapper and report the inner categories' titles.
        # If multiple roots exist (unusual), union them.
        out: set[str] = set()
        for node in schema:
            if isinstance(node, dict):
                children = node.get("categories", []) or []
                for c in children:
                    if isinstance(c, dict) and c.get("title"):
                        out.add(c["title"])
        return out
    if isinstance(schema, dict):
        children = schema.get("categories", []) or []
        return {c["title"] for c in children if isinstance(c, dict) and c.get("title")}
    return set()


def _merge_category_schema(
    existing: Any,
    desired_titles: list[str],
) -> Any:
    """Append missing desired-title categories to the schema's top level.

    Preserves any pre-existing nodes (and their children) untouched.
    Returns a new structure matching the input shape; doesn't mutate
    the input.

    For the common ``[{"title": "Categories", "categories": [...]}]``
    shape, the new categories are appended inside that root node so
    they appear as siblings of the existing top-level categories.
    """
    if isinstance(existing, list) and existing and isinstance(existing[0], dict):
        # Wrapped-root form. Copy the root, append new categories
        # under its 'categories' key.
        root = dict(existing[0])
        root["categories"] = list(root.get("categories", []) or [])
        existing_titles = {
            c["title"] for c in root["categories"]
            if isinstance(c, dict) and c.get("title")
        }
        for title in desired_titles:
            if title not in existing_titles:
                root["categories"].append({"title": title, "categories": []})
        # Preserve any additional roots untouched (rare).
        return [root] + list(existing[1:])

    # Plain-dict form (older SDK) — preserve for back-compat.
    out = {
        "categories": list(
            (existing or {}).get("categories", []) or []
        ),
    }
    existing_top_titles = {
        c.get("title") for c in out["categories"] if isinstance(c, dict)
    }
    for title in desired_titles:
        if title not in existing_top_titles:
            out["categories"].append({"title": title, "categories": []})
    return out


# ----------------------------------------------------------------------------
# push() — the catalogue → AGOL write path (Phase B)
# ----------------------------------------------------------------------------

# Sync statuses that allow a push attempt. 'pending_pull' and
# 'conflict' must be resolved first (Phase D — they indicate AGOL-side
# drift that the steward needs to triage). 'clean' rows are
# re-pushable: the push is idempotent (metadata + sharing + source
# reconcile all converge to the same end state) and the steward may
# want to force a re-sync for verification or after a manual AGOL-UI
# edit they want overwritten.
_PUSHABLE_STATUSES: frozenset[str] = frozenset({
    "unpublished", "pending_push", "error", "clean",
})

# Allowed (publish-target, source-format) pairs. Anything else surfaces
# as a clean validation error before any AGOL contact.
_VALID_TARGET_FORMAT: dict[str, set[str]] = {
    "feature-layer":      {"geopackage"},
    "vector-tile-layer":  {"geopackage"},
    "imagery-layer":      {"geotiff"},
}


def push(
    db_path: Path,
    dataset_id: str,
    gis,
    config: AgolConfig,
    *,
    library_root: Path,
    actor: str,
    target_override: str | None = None,
    sharing_override: str | None = None,
    dry_run: bool = False,
    cache_dir: Path | None = None,
) -> SyncResult:
    """Push a single catalogue row to AGOL.

    Resolves the publish target (CLI ``target_override`` wins, else
    ``row['agol_format']``), branches by target type, generates a
    thumbnail, sets sharing (org + Conservation Atlas group unless
    overridden), and updates the catalogue + changelog on success.

    Args:
        db_path: Path to inventory.db.
        dataset_id: The row's ``dataset_id`` (ULID).
        gis: A live ``arcgis.gis.GIS`` connection.
        config: Loaded AgolConfig (provides folder prefix, group
            name, etc.).
        library_root: Typed library root (``library/spatial``).
        actor: Recorded in the changelog as the sync's actor.
        target_override: If set, overrides the row's ``agol_format``
            for this single push. Recorded in the changelog so the
            ad-hoc choice is auditable.
        sharing_override: One of ``private`` / ``org`` / ``public``
            (or ``None`` for the default org+Conservation-Atlas-group).
        dry_run: If True, computes the planned payload + returns a
            SyncResult marked ``action='push (dry-run)'`` without
            touching AGOL or the catalogue.
        cache_dir: Override the thumbnail/VTPK cache root
            (``.y2y`` under the project root by default).

    Returns:
        ``SyncResult`` describing the outcome and the new
        ``sync_status``.

    Raises:
        AgolError / AgolToolingError / AgolGroupNotFoundError:
            for unrecoverable failures. The caller (CLI or batch
            wrapper) decides how to surface these to the steward.
    """
    from . import inventory_manager
    from .utils import utc_now_iso

    cache_dir = cache_dir or (Path.home() / ".y2y")

    # ----- read + validate the row -------------------------------------
    row = inventory_manager.get_dataset(db_path, dataset_id)
    if row is None:
        raise AgolError(f"dataset_id {dataset_id!r} not in catalogue")

    if row.get("status") != "active":
        raise AgolError(
            f"refusing to push {dataset_id!r}: status is "
            f"{row.get('status')!r}, not 'active'"
        )

    sync_status_before = row.get("sync_status") or "unpublished"
    if sync_status_before not in _PUSHABLE_STATUSES:
        raise AgolError(
            f"refusing to push {dataset_id!r}: sync_status is "
            f"{sync_status_before!r}; resolve via pull or unpublish first."
        )

    target = target_override or row.get("agol_format")
    if target not in _VALID_TARGET_FORMAT:
        raise AgolError(
            f"unknown agol_format {target!r} on {dataset_id!r}. "
            f"Expected one of: {sorted(_VALID_TARGET_FORMAT)}"
        )
    source_format = row.get("format")
    allowed_formats = _VALID_TARGET_FORMAT[target]
    if source_format not in allowed_formats:
        raise AgolError(
            f"agol_format {target!r} is not valid for format "
            f"{source_format!r} (allowed: {sorted(allowed_formats)})."
        )

    # ----- compute the AGOL-side payload -------------------------------
    properties = compute_item_properties(row)
    folder = compute_target_folder(row["category"])
    source_folder = compute_sources_folder()
    sharing_payload = _resolve_sharing(config, gis, sharing_override)

    # Pre-create the AGOL folder(s) we'll need so item.move()
    # succeeds. AGOL's Item.move() requires the target folder to
    # already exist (gis.content.add() auto-creates, but move()
    # doesn't). The _ensure_folder helper is idempotent
    # (exist_ok=True) and returns the live Folder instance — we
    # hand that instance (rather than the bare name) to _safe_move
    # so the SDK doesn't have to look the folder up by name (a path
    # that fails silently if AGOL hasn't yet indexed the
    # just-created folder).
    #
    # Imagery layers publish via arcgis.raster.publish_hosted_imagery_layer
    # in the no-source model (steward-confirmed 2026-05-22): no
    # separate source TIFF item ever exists, so we skip pre-creating
    # the _sources folder for imagery rows. Vectors + VTL still need
    # both folders.
    folder_obj: Any = folder
    source_folder_obj: Any = source_folder
    if not dry_run:
        folder_obj = _ensure_folder(gis, folder) or folder
        if target != "imagery-layer":
            source_folder_obj = _ensure_folder(gis, source_folder) or source_folder

    # ----- dry-run short-circuit ---------------------------------------
    if dry_run:
        note = _format_push_plan(
            target=target,
            folder=folder,
            properties=properties,
            sharing_payload=sharing_payload,
            row=row,
            target_override_used=bool(target_override),
        )
        return SyncResult(
            dataset_id=dataset_id,
            action="push (dry-run)",
            sync_status_before=sync_status_before,
            sync_status_after=sync_status_before,
            agol_item_id=row.get("agol_item_id"),
            note=note,
        )

    # ----- pre-flight: thumbnail (per target) --------------------------
    from . import agol_thumbnails

    try:
        thumb_path = agol_thumbnails.generate_thumbnail(
            row, library_root, cache_dir,
        )
    except agol_thumbnails.ThumbnailError as exc:
        # Continue without a thumbnail rather than failing the push;
        # AGOL handles missing thumbnails fine. Note this in the
        # changelog so the steward can investigate later.
        thumb_path = None
        _record_warning(properties, f"thumbnail generation failed: {exc}")

    # ----- target-switch detection --------------------------------------
    # If the catalogue's agol_item_id points to an AGOL item whose
    # type doesn't match the catalogue's current agol_format, the
    # steward has changed their mind about how this dataset should
    # be published (e.g., FL → VTL). Unpublish the old AGOL items
    # before proceeding with the create path for the new target.
    #
    # AGOL-missing-out-of-band is also handled here: if the item is
    # gone, we clear the catalogue link silently and create fresh.
    existing_item_id_before = row.get("agol_item_id")
    if existing_item_id_before:
        switch_result = _handle_target_switch(
            gis=gis,
            dataset_id=dataset_id,
            existing_item_id=existing_item_id_before,
            target=target,
            actor=actor,
            properties=properties,
        )
        if switch_result is not None:
            # A target switch (or missing-out-of-band) was handled.
            # Mutate the row dict's agol_item_id so the per-target
            # helper below takes the create path with no prior ID.
            row = dict(row)
            row["agol_item_id"] = None

    # ----- per-target publish path -------------------------------------
    source_path = library_root / row["file_path"]
    if not source_path.exists():
        raise AgolError(
            f"source file missing for {dataset_id!r}: {source_path}"
        )

    if target == "feature-layer":
        item = _publish_feature_layer(
            gis=gis, source_path=source_path,
            properties=properties,
            folder=folder, source_folder=source_folder,
            folder_obj=folder_obj, source_folder_obj=source_folder_obj,
            dataset_id=dataset_id,
            existing_item_id=row.get("agol_item_id"),
            checksum_changed=_checksum_changed(row),
        )
    elif target == "vector-tile-layer":
        # Rev 3: VTPK is a manually-built artifact at the canonical
        # library/vtpk/ location. Push requires it to be present
        # (ingested via `y2y ingest scan`). If absent, error with
        # actionable guidance; the catalogue row stays unchanged
        # and the steward can retry after ingesting the VTPK.
        from . import agol_vtpk as _agol_vtpk
        vtpk_path = _agol_vtpk.resolve_vtpk_path(row, library_root)
        if not vtpk_path.exists():
            raise AgolError(
                f"No VTPK ingested for {dataset_id!r} (expected at "
                f"{vtpk_path.relative_to(library_root.parent)}). "
                f"Build the VTPK in ArcGIS Pro from {source_path}, "
                f"drop the resulting `.vtpk` in queue/incoming/, "
                f"then run `y2y ingest`. After that, re-run "
                f"this push."
            )
        item = _publish_vector_tile_layer(
            gis=gis, vtpk_path=vtpk_path,
            properties=properties,
            folder=folder, source_folder=source_folder,
            folder_obj=folder_obj, source_folder_obj=source_folder_obj,
            existing_item_id=row.get("agol_item_id"),
            dataset_id=dataset_id,
        )
    elif target == "imagery-layer":
        item = _publish_imagery_layer(
            gis=gis, source_path=source_path,
            properties=properties,
            folder=folder, source_folder=source_folder,
            folder_obj=folder_obj, source_folder_obj=source_folder_obj,
            dataset_id=dataset_id,
            service_name=compute_service_name(row),
            existing_item_id=row.get("agol_item_id"),
            checksum_changed=_checksum_changed(row),
        )
    else:
        # Should be unreachable given the validation above.
        raise AgolError(f"internal: unhandled target {target!r}")

    # ----- thumbnail attach (post-publish) -----------------------------
    if thumb_path is not None:
        try:
            item.update(thumbnail=str(thumb_path))
        except Exception as exc:  # pragma: no cover — best-effort
            # Don't fail the whole push if the thumbnail upload alone
            # bombs. Note in the changelog.
            _record_warning(
                properties,
                f"thumbnail upload failed post-publish: {exc}",
            )

    # ----- sharing -----------------------------------------------------
    _apply_sharing(item, sharing_payload)

    # ----- catalogue update --------------------------------------------
    now = utc_now_iso()
    updates: dict[str, Any] = {
        "agol_item_id": item.id,
        "last_synced_at": now,
        "sync_status": "clean",
    }
    if not row.get("agol_published_at"):
        updates["agol_published_at"] = now

    # Collect every [agol] warning recorded during this push so the
    # steward sees them all in one place rather than the last-write-
    # wins behaviour of the prior scalar-key model.
    annotation_parts: list[str] = [
        f"[agol] {w}" for w in properties.pop("_warnings", [])
    ]
    if annotation_parts:
        prior = row.get("internal_notes") or ""
        annotation = "\n".join(annotation_parts)
        updates["internal_notes"] = (
            f"{prior}\n{annotation}".strip() if prior else annotation
        )

    inventory_manager.update_dataset(db_path, dataset_id, updates)

    # ----- changelog ---------------------------------------------------
    note_parts = [
        f"pushed to AGOL as {target}",
        f"item_id={item.id}",
        f"folder={folder}",
    ]
    if target_override:
        note_parts.append(f"target overridden via CLI flag")
    if sharing_override:
        note_parts.append(f"sharing overridden via CLI flag: {sharing_override}")
    if annotation_parts:
        note_parts.extend(annotation_parts)
    note = " | ".join(note_parts)

    inventory_manager.append_changelog(
        db_path,
        timestamp=now,
        action="metadata",
        dataset_id=dataset_id,
        actor=actor,
        path=row.get("file_path"),
        detail=note,
        field_changed="sync_status",
        old_value=sync_status_before,
        new_value="clean",
    )

    return SyncResult(
        dataset_id=dataset_id,
        action="push",
        sync_status_before=sync_status_before,
        sync_status_after="clean",
        agol_item_id=item.id,
        note=note,
    )


# --- per-target publish helpers ----------------------------------------
#
# AGOL's hosted-services model is two-item: each published service has
# a paired source item (the GPKG / GeoTIFF / VTPK file that was
# originally uploaded) and the service item (the Feature Service /
# Imagery Service / Vector Tile Service that users actually consume in
# maps).
#
# The catalogue's ``agol_item_id`` always points at the SERVICE item
# (the user-facing one). To do a data refresh, the integration finds
# the corresponding source via ``service_item.related_items('Service2Data',
# 'forward')`` and updates the data through the source. For
# feature-layer rows the modern arcgis SDK exposes a more direct path:
# ``FeatureLayerCollection.fromitem(service).manager.overwrite(...)``
# which replaces the service's underlying data without going through
# the source item.
#
# Why not just call ``service.publish(file_type=..., overwrite=True)``?
# Because that's the wrong shape for a service item — AGOL interprets
# it as "create a new service derived from this service", which
# produces a duplicate item with default sharing and no categories
# (the failure mode that test #1 surfaced before this fix landed).


# Source items (GPKGs / COGs / VTPKs that feed published services) are
# pipeline plumbing — not user-facing. They go to a dedicated folder
# (`<prefix>/_sources/`) so the category-mirroring folder tree stays
# clean, get the minimum metadata needed for steward findability +
# audit, and inherit AGOL's default private sharing (the integration
# never calls _apply_sharing on a source item). Steward-confirmed
# design 2026-05-22.
_SOURCES_FOLDER_NAME = "_sources"

_SOURCE_DESCRIPTION = (
    "Source data for the hosted service. Managed by Y2Y pipeline; "
    "do not edit directly. The user-facing item is the hosted "
    "service derived from this source."
)


def compute_sources_folder() -> str:
    """Return the AGOL folder for source items: ``_sources``.

    All source items (GPKG / COG / VTPK) land in this single flat
    folder regardless of catalogue category, keeping the
    category-mirroring folders (``Species/``, ``Water/``, …) clean of
    plumbing items.

    Returns the bare folder name because AGOL folders are a flat
    namespace — see :func:`compute_target_folder` for context.
    """
    return _SOURCES_FOLDER_NAME


def _ensure_folder(gis, folder_name: str) -> Any | None:
    """Idempotently ensure ``folder_name`` exists; return its Folder instance.

    AGOL's ``Item.move(folder=...)`` requires the target folder to
    already exist — unlike ``gis.content.add(folder=...)`` which
    auto-creates. We touch the folder explicitly before any move()
    call so the move succeeds even on a fresh org / first-ever push
    into a category.

    Returning the live ``Folder`` instance matters because
    ``Item.move()`` is more reliable when given the instance directly
    rather than the folder name. When given a string, the SDK does
    ``gis.content.folders.get(folder=name)._fid`` internally — if
    ``get()`` returns ``None`` (folder name with special characters,
    AGOL hasn't indexed a just-created folder yet, etc.), that
    crashes with AttributeError. Passing the Folder instance skips
    the lookup entirely.

    Idempotency: ``exist_ok=True`` makes ``Folders.create()`` return
    the existing Folder rather than raising ``FolderException``. SDK
    2.3+.

    Returns ``None`` if folder creation fails (e.g., legacy SDK
    without the folders manager). Callers fall back to passing the
    folder name string to ``Item.move()``, which will surface its
    own error via the success-flag check in ``_safe_move``.
    """
    try:
        return gis.content.folders.create(folder=folder_name, exist_ok=True)
    except Exception:
        return None


def _record_warning(properties: dict[str, Any], message: str) -> None:
    """Append a [agol] warning message to be surfaced in changelog + internal_notes.

    Stored as a list under ``properties['_warnings']`` so multiple
    independent warnings from a single push (thumbnail failure +
    move failure + source-reconcile failure, etc.) all reach the
    steward instead of overwriting each other.
    """
    properties.setdefault("_warnings", []).append(message)


def _safe_move(item: Any, folder: Any, properties: dict[str, Any]) -> None:
    """Move ``item`` to ``folder`` and record any failure visibly.

    AGOL's ``Item.move()`` doesn't raise on server-side failure — it
    returns a JSON dict ``{"success": true|false, "itemId": ..., ...}``
    and a ``success: false`` payload looks identical to a success
    from the caller's perspective. The previous implementation only
    caught Python exceptions, so an item that AGOL refused to move
    silently stayed in My Content root with no diagnostic. We now
    check the return value's ``success`` flag explicitly.

    ``folder`` is either a string folder name OR a ``Folder`` instance
    (as returned by ``_ensure_folder``). Prefer the instance —
    ``Item.move()`` resolves a string by calling
    ``gis.content.folders.get(folder=name)._fid`` internally, which
    crashes with AttributeError if AGOL's folder index hasn't caught
    up with a just-created folder.

    Failures are captured via ``_record_warning`` so they propagate
    to ``internal_notes`` and the changelog via push()'s annotation
    collection.
    """
    folder_label = getattr(folder, "name", None) or str(folder)
    try:
        result = item.move(folder=folder)
    except Exception as exc:
        _record_warning(properties, (
            f"AGOL item.move to folder {folder_label!r} failed; item "
            f"may be in My Content root. Move manually from the AGOL "
            f"UI or re-push. Underlying: {exc}"
        ))
        return
    # Defensive: explicit success-flag check. AGOL signals failure
    # via the payload, not via an exception.
    if isinstance(result, dict) and result.get("success") is False:
        _record_warning(properties, (
            f"AGOL item.move to folder {folder_label!r} returned "
            f"success=False; item is still in My Content root. AGOL "
            f"payload: {result!r}. Move manually from the AGOL UI or "
            f"re-push."
        ))
    elif result is None:
        # Item.move() returns None when it can't resolve the folder
        # arg (logs "Folder not found for given owner" to stdout and
        # bails). The item didn't move.
        _record_warning(properties, (
            f"AGOL item.move to folder {folder_label!r} returned "
            f"None — the SDK could not resolve the target folder. "
            f"Item is still in My Content root."
        ))


def _reconcile_source_item(
    source_item: Any,
    dataset_id: str,
    *,
    source_folder: str,
    source_folder_obj: Any = None,
    source_type: str,
    service_properties: dict[str, Any],
    properties: dict[str, Any],
    extra_type_keywords: tuple[str, ...] = (),
) -> None:
    """Enforce the minimal-source policy on an existing source item.

    Called on every push UPDATE path so source items stay in
    compliance regardless of what manual configuration or legacy
    state they were in. Steward-confirmed behaviour 2026-05-22:
    "check them with each push (all scenarios) and move the source
    files to the correct folder and make sure the source file
    metadata matches what it should."

    What this enforces:

    * **Folder**: ``_sources``. If the source sits in a
      category folder or anywhere else, ``_safe_move`` relocates it.
    * **Item properties**: title aligned with the service, description
      stub, typeKeywords set to ``['Y2Y', 'Y2Y:source',
      'Y2Y:dataset_id:<id>']``. Public-facing fields (``categories``,
      ``tags``, ``snippet``, ``accessInformation``, ``licenseInfo``)
      are explicitly cleared — those belong on the service item only.
    * **Sharing**: forced to private (owner-only). Sources are
      pipeline plumbing and shouldn't appear in any gallery search.

    Idempotent on a compliant source (re-runs are AGOL-side no-ops
    plus a couple of redundant API calls). Failures are recorded as
    ``_warnings`` entries but never abort the push — the service-side
    state is more important than perfect source hygiene.
    """
    # Build the minimal-policy properties dict, then explicitly clear
    # any public-facing fields that may exist from manual setup.
    target_props = _compute_source_item_properties(
        service_properties, dataset_id, source_type=source_type,
    )
    target_props.update({
        "categories": [],
        "tags": [],
        "snippet": "",
        "accessInformation": "",
        "licenseInfo": "",
    })
    # Rev 3: callers (e.g. the VTL update path) can attach extra
    # bookkeeping typeKeywords to the minimal-source policy props.
    # The VTL helper uses this to stamp/refresh ``Y2Y:vtpk_sha256:<hex>``
    # so subsequent pushes can detect VTPK changes without
    # re-downloading the package.
    if extra_type_keywords:
        existing = list(target_props.get("typeKeywords") or [])
        # Drop any prior values that share the same key prefix
        # (e.g. an old vtpk_sha256 from a stale push) so the new
        # one replaces them cleanly.
        prefixes = {kw.split(":")[0] + ":" + kw.split(":")[1] + ":"
                    for kw in extra_type_keywords if kw.count(":") >= 2}
        existing = [
            kw for kw in existing
            if not any(kw.startswith(p) for p in prefixes)
        ]
        existing.extend(extra_type_keywords)
        target_props["typeKeywords"] = existing

    # Move first (if needed) so subsequent property updates land on
    # the item at its new location. Prefer the Folder instance (more
    # reliable in the SDK), fall back to the string name.
    _safe_move(
        source_item, source_folder_obj or source_folder, properties,
    )

    # Apply the minimal-policy properties.
    try:
        source_item.update(item_properties=target_props)
    except Exception as exc:
        _record_warning(properties, (
            f"failed to reconcile source item {getattr(source_item, 'id', '?')!r} "
            f"metadata to minimal-source policy: {exc}"
        ))

    # Force private sharing — the source should never be gallery-visible.
    sharing = getattr(source_item, "sharing", None)
    if sharing is not None and hasattr(sharing, "sharing_level"):
        try:
            sharing.sharing_level = "PRIVATE"
        except Exception as exc:
            _record_warning(properties, (
                f"failed to set source item sharing to PRIVATE: {exc}"
            ))


def _compute_source_item_properties(
    service_properties: dict[str, Any],
    dataset_id: str,
    *,
    source_type: str,
) -> dict[str, Any]:
    """Minimal AGOL item property dict for a source (GPKG / COG / VTPK).

    Mirrors only what's needed:

    * ``type`` — AGOL item type ("GeoPackage" / "Image" / "Vector Tile
      Package") so AGOL knows what kind of binary this is.
    * ``title`` — same as the service so the steward can find the
      source pair in their content tree.
    * ``description`` — short stub flagging this as pipeline plumbing.
    * ``typeKeywords`` — ``Y2Y``, ``Y2Y:source``,
      ``Y2Y:dataset_id:<id>``. No ``Y2Y:category:<cat>`` keyword —
      sources don't carry catalogue category information.

    Deliberately omitted (kept on the service item only):

    * ``snippet`` (summary)
    * ``tags``
    * ``accessInformation`` (steward acknowledgements)
    * ``licenseInfo`` (terms of use)
    * ``categories`` (AGOL Content Categories — the typology facet)

    Sharing is not part of item properties — it's applied separately,
    and ``push()`` only applies sharing to the service item, never
    the source.
    """
    return {
        "type": source_type,
        "title": service_properties.get("title"),
        "description": _SOURCE_DESCRIPTION,
        "typeKeywords": [
            "Y2Y",
            "Y2Y:source",
            f"Y2Y:dataset_id:{dataset_id}",
        ],
    }


def _resolve_service_item(gis, agol_item_id: str) -> Any:
    """Fetch the service item by ID; raise a clear error if missing."""
    item = gis.content.get(agol_item_id)
    if item is None:
        raise AgolError(
            f"agol_item_id {agol_item_id!r} no longer exists on AGOL "
            f"(deleted out-of-band?). Use `y2y agol-sync unpublish` to "
            f"clear the catalogue link, then re-push."
        )
    return item


# AGOL item types corresponding to each agol_format. push()'s
# target-switch detection compares the catalogue's intent against
# the AGOL item's actual type to decide whether to unpublish + re-
# publish under the new target.
_TARGET_TO_AGOL_TYPE: dict[str, str] = {
    "feature-layer": "Feature Service",
    "vector-tile-layer": "Vector Tile Service",
    "imagery-layer": "Image Service",
}


def _handle_target_switch(
    *,
    gis,
    dataset_id: str,
    existing_item_id: str,
    target: str,
    actor: str,
    properties: dict[str, Any],
) -> str | None:
    """If the catalogue's agol_format no longer matches the AGOL item, unpublish.

    Steward-confirmed (2026-05-27): when an existing row's
    ``agol_format`` is changed (e.g. ``feature-layer`` →
    ``vector-tile-layer``), the next push should delete the old
    AGOL representation before publishing the new one. AGOL credit
    cost is one of the reasons the steward chose VTL — keeping the
    old FS around would defeat that.

    This function is called by ``push()`` before the per-target
    publish dispatch. It compares the catalogue's intended
    ``target`` against the actual AGOL item's ``.type`` and, on
    mismatch, deletes the service + any Service2Data-linked source
    items, then returns a short note (which the caller uses to
    update changelog metadata and to clear the catalogue's
    ``agol_item_id`` so the create path takes over).

    Also handles the case where the AGOL item is missing entirely
    (deleted out-of-band): returns a note about that and the
    caller clears the link the same way.

    Returns ``None`` when no switch is needed (the item exists and
    its type matches the target).
    """
    item = gis.content.get(existing_item_id)
    if item is None:
        msg = (
            f"agol_item_id {existing_item_id!r} no longer exists on "
            f"AGOL (deleted out-of-band). Clearing catalogue link; "
            f"create-path will fire."
        )
        _record_warning(properties, msg)
        return msg

    expected_type = _TARGET_TO_AGOL_TYPE.get(target)
    actual_type = getattr(item, "type", None)
    if expected_type is not None and actual_type == expected_type:
        return None  # No switch needed.

    # Target switch detected. Delete the old service + its linked
    # sources to free credits before publishing the new target.
    note = (
        f"agol_format switched (AGOL item is {actual_type!r}, "
        f"catalogue wants {expected_type!r}). Unpublishing the "
        f"existing AGOL items before publishing the new target."
    )
    _record_warning(properties, note)
    _unpublish_agol_items(
        gis=gis, service_item=item, dataset_id=dataset_id,
        properties=properties,
    )
    return note


def _unpublish_agol_items(
    *,
    gis,
    service_item: Any,
    dataset_id: str,
    properties: dict[str, Any],
) -> None:
    """Delete a service item and any Service2Data-linked source items.

    Used by the target-switch path. Phase E's ``unpublish()``
    command will share this helper for the explicit
    ``y2y agol-sync unpublish`` workflow.

    ``permanent=True`` is critical here: without it, AGOL sends
    the item to the recycle bin and keeps the service name
    reservation alive. The next publish on the same service name
    then fails with "Service name '...' already exists for '<userid>'".
    For target-switch we want the name freed immediately so the
    new representation can be published.

    Failures are recorded as ``[agol]`` warnings rather than
    aborting — partial cleanup is better than leaving the catalogue
    in an inconsistent state with no remediation message. The
    catalogue's ``agol_item_id`` is cleared by the caller after
    this returns; subsequent reconcile / push runs will surface
    any orphans on the AGOL side.
    """
    # Find and delete linked source(s) first. Walking Service2Data
    # before deleting the service preserves the relationship while
    # we still have a valid handle.
    try:
        linked = service_item.related_items(
            rel_type="Service2Data", direction="forward",
        ) or []
    except Exception as exc:
        _record_warning(properties, (
            f"could not enumerate source items linked to "
            f"{getattr(service_item, 'id', '?')!r}: {exc}"
        ))
        linked = []

    for src in linked:
        try:
            src.delete(permanent=True)
        except TypeError:
            # SDK pre-2.4 didn't expose permanent= kwarg; fall back.
            try:
                src.delete()
            except Exception as exc:
                _record_warning(properties, (
                    f"failed to delete linked source item "
                    f"{getattr(src, 'id', '?')!r} during target "
                    f"switch: {exc}"
                ))
        except Exception as exc:
            _record_warning(properties, (
                f"failed to delete linked source item "
                f"{getattr(src, 'id', '?')!r} during target switch: {exc}"
            ))

    # Now delete the service itself, also permanently so the name
    # frees up for the new target's publish.
    try:
        service_item.delete(permanent=True)
    except TypeError:
        try:
            service_item.delete()
        except Exception as exc:
            _record_warning(properties, (
                f"failed to delete service item "
                f"{getattr(service_item, 'id', '?')!r} during target "
                f"switch: {exc}"
            ))
    except Exception as exc:
        _record_warning(properties, (
            f"failed to delete service item "
            f"{getattr(service_item, 'id', '?')!r} during target "
            f"switch: {exc}"
        ))


def _find_source_item(service_item: Any) -> Any | None:
    """Walk Service2Data forward to find the source item for a service.

    Returns ``None`` if the relationship isn't present (e.g., the
    service was created outside our integration via a manual upload
    workflow that didn't preserve the Service2Data link). Callers
    decide how to handle that case.
    """
    try:
        related = service_item.related_items(
            rel_type="Service2Data", direction="forward"
        ) or []
    except Exception:
        return None
    return related[0] if related else None


def _publish_feature_layer(
    *,
    gis,
    source_path: Path,
    properties: dict[str, Any],
    folder: str,
    source_folder: str,
    folder_obj: Any = None,
    source_folder_obj: Any = None,
    dataset_id: str,
    existing_item_id: str | None,
    checksum_changed: bool,
) -> Any:
    """Publish (or update) a Hosted Feature Layer from a GPKG source.

    Returns the **service** item (Feature Service) — that's what the
    catalogue tracks as ``agol_item_id``.
    """
    item_props = _strip_internal(properties)

    if existing_item_id is None:
        # ----- create path -----
        # Source items get minimal metadata + the dedicated _sources
        # folder + AGOL's default private sharing (we never call
        # _apply_sharing on a source). The service item gets the full
        # steward-authored metadata + the category folder + the
        # configured sharing (applied by push() after this helper
        # returns).
        source_props = _compute_source_item_properties(
            item_props, dataset_id, source_type="GeoPackage",
        )
        # Use Folder.add() (new SDK API) rather than the deprecated
        # gis.content.add() to sidestep the arcgis 2.4.x lazy-loader
        # bug — same fix as the VTL path. See _add_item_to_folder.
        gpkg_item = _add_item_to_folder(
            gis=gis,
            source_props=source_props,
            file_path=source_path,
            source_folder=source_folder,
            source_folder_obj=source_folder_obj,
            properties=properties,
        )
        try:
            service_item = gpkg_item.publish(file_type="GeoPackage")
        except Exception as exc:
            _record_warning(properties, (
                f"feature-layer publish failed; item retained as "
                f"downloadable GeoPackage instead. Underlying: {exc}"
            ))
            # Fallback: the source IS now the user-facing item.
            # Upgrade it: move from _sources to the category folder,
            # apply the full steward-authored metadata. push() will
            # then apply sharing to it as if it were the service.
            _safe_move(gpkg_item, folder_obj or folder, properties)
            gpkg_item.update(item_properties=item_props)
            return gpkg_item
        # Normal path: hosted service was published. Move it to the
        # category folder (publish() places it in My Content root by
        # default) and apply the full metadata.
        _safe_move(service_item, folder_obj or folder, properties)
        service_item.update(item_properties=item_props)
        return service_item

    # ----- update path -----
    service = _resolve_service_item(gis, existing_item_id)
    source = _find_source_item(service)

    # Data refresh first (before metadata reapply), so any service-
    # metadata side effects of the publish are overridden by our
    # explicit update below. Two AGOL patterns for hosted feature
    # layers — pick the right SDK call based on whether the service
    # has a linked source item:
    #
    # * Linked FS (has Service2Data forward → source GPKG item):
    #   update source.data + source.publish(overwrite=True). AGOL
    #   tracks the source-service linkage and refreshes the existing
    #   service in place.
    #
    # * Self-hosted FS (no source item): FeatureLayerCollection
    #   .fromitem(service).manager.overwrite(<file>). AGOL accepts
    #   the file as the new backing data for the service directly.
    #
    # Using FLC.overwrite on a linked FS triggers AGOL Error 500
    # ("this data is already referring to another service") because
    # AGOL refuses to attach a separate data blob to a service that
    # already has a linked source.
    if checksum_changed:
        if source is not None:
            try:
                source.update(data=str(source_path))
                source.publish(
                    file_type="GeoPackage", overwrite=True,
                )
            except Exception as exc:
                _record_warning(properties, (
                    f"feature-layer source-publish refresh failed; "
                    f"metadata updated but service data may be stale. "
                    f"Underlying: {exc}"
                ))
        else:
            try:
                from arcgis.features import FeatureLayerCollection
                flc = FeatureLayerCollection.fromitem(service)
                flc.manager.overwrite(str(source_path))
            except Exception as exc:
                _record_warning(properties, (
                    f"FeatureLayerCollection.overwrite failed; metadata "
                    f"updated but the service's data may be stale. "
                    f"Underlying: {exc}"
                ))

    # Apply / re-apply the full steward-authored metadata to the
    # service. Idempotent — running this on an already-correct
    # service is a no-op AGOL-side. Required *after* any data
    # refresh in case AGOL's publish() reset any service-level
    # properties.
    service.update(item_properties=item_props)

    # Enforce folder placement on every push, not just on create.
    # Services published in prior runs (or by hand in the AGOL UI)
    # may be sitting in My Content root or the wrong category folder.
    # Item.move() is idempotent server-side — if the service is
    # already in the target folder, AGOL returns success and nothing
    # changes. Without this, an item that started life in root stays
    # in root forever because the update path used to only refresh
    # data + metadata.
    _safe_move(service, folder_obj or folder, properties)

    # Reconcile the source item on every push (steward-confirmed
    # 2026-05-22): ensures it sits in _sources with minimal metadata
    # + private sharing regardless of any prior manual configuration.
    # Runs *after* any data refresh so the source's data is current
    # before we strip its metadata.
    if source is not None:
        _reconcile_source_item(
            source, dataset_id,
            source_folder=source_folder,
            source_folder_obj=source_folder_obj,
            source_type="GeoPackage",
            service_properties=item_props,
            properties=properties,
        )
    return service


@contextmanager
def _suppress_insecure_request_warnings():
    """Context manager that silences urllib3's InsecureRequestWarning.

    The arcgis SDK's hosted imagery publish flow polls AGOL's raster
    analysis service (``rasteranalysis*.arcgis.com``) repeatedly
    while the server processes the upload. Those internal SDK
    requests are made with ``verify=False``, so urllib3 emits an
    ``InsecureRequestWarning`` on every poll. For a multi-minute
    publish of a single raster, that's easily 1000+ warnings dumped
    to stderr — drowning out real signal.

    We can't fix the SDK's choice (it's Esri-internal traffic), and
    we don't want to disable the warning globally (it's a legitimate
    signal in other contexts), so we suppress it narrowly at the
    publish_hosted_imagery_layer call sites only.
    """
    import warnings
    from urllib3.exceptions import InsecureRequestWarning
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=InsecureRequestWarning)
        yield


def _publish_imagery_layer(
    *,
    gis,
    source_path: Path,
    properties: dict[str, Any],
    folder: str,
    source_folder: str,
    folder_obj: Any = None,
    source_folder_obj: Any = None,
    dataset_id: str,
    service_name: str,
    existing_item_id: str | None,
    checksum_changed: bool,
) -> Any:
    """Publish (or update) a Hosted Imagery Layer from a GeoTIFF source.

    **No-source model** (steward-confirmed 2026-05-22): rasters
    publish as a hosted imagery layer with **no separate source
    TIFF item** in the catalogue's My Content tree. The end state
    matches AGOL's web-UI "Create hosted imagery layer" flow:
    just one Imagery Layer item, no item in ``_sources/``.

    Implementation: ``arcgis.raster.publish_hosted_imagery_layer``
    uploads the local TIFF directly into AGOL's internal raster
    store (Azure blob backend) and creates a hosted imagery layer
    in one operation. The raster lives inside AGOL's managed
    infrastructure, not as a user-visible item in the steward's
    content browser.

    Data refresh: re-call the same function with ``output_name``
    set to the existing service item. AGOL replaces the underlying
    raster in place, preserving item ID, URL, sharing, categories.

    Source-related code paths (``_compute_source_item_properties``,
    ``_reconcile_source_item``, ``_find_source_item``) are
    deliberately not invoked for the imagery path — there's no
    source to reconcile.

    Returns the **service** item (Imagery Layer).
    """
    from arcgis.raster import publish_hosted_imagery_layer

    item_props = _strip_internal(properties)

    if existing_item_id is None:
        # ----- create path -----
        # publish_hosted_imagery_layer uploads the TIFF to AGOL's
        # raster store and creates the imagery service. Returns the
        # Imagery Layer Item directly. No intermediate source item.
        try:
            with _suppress_insecure_request_warnings():
                service_item = publish_hosted_imagery_layer(
                    input_data=[str(source_path)],
                    layer_configuration="ONE_IMAGE",
                    tiles_only=False,
                    # AGOL service names allow only [A-Za-z0-9_], so
                    # we pass the sanitised name (compute_service_name).
                    # The human-readable title is preserved via
                    # item.update(item_properties=...) below.
                    output_name=service_name,
                    gis=gis,
                )
        except Exception as exc:
            raise AgolError(
                f"hosted imagery publish failed for {dataset_id!r}: {exc}"
            ) from exc

        # Move to the category folder + apply the full
        # steward-authored metadata.
        _safe_move(service_item, folder_obj or folder, properties)
        service_item.update(item_properties=item_props)
        return service_item

    # ----- update path -----
    service = _resolve_service_item(gis, existing_item_id)

    # Data refresh: re-publish with output_name=<existing service>.
    # AGOL replaces the raster in place, preserves item ID + URL +
    # sharing + categories. Runs before metadata reapply so any
    # publish side effects on item properties are overridden by our
    # explicit update below.
    if checksum_changed:
        try:
            with _suppress_insecure_request_warnings():
                publish_hosted_imagery_layer(
                    input_data=[str(source_path)],
                    layer_configuration="ONE_IMAGE",
                    tiles_only=False,
                    output_name=service,  # existing Item → replace in place
                    gis=gis,
                )
        except Exception as exc:
            _record_warning(properties, (
                f"imagery data refresh failed; metadata updated but "
                f"service data may be stale. Underlying: {exc}"
            ))

    # Apply / re-apply full steward-authored metadata to the service.
    service.update(item_properties=item_props)

    # Enforce folder placement on every push (idempotent — same
    # rationale as in _publish_feature_layer's update path).
    _safe_move(service, folder_obj or folder, properties)

    return service


def _publish_vector_tile_layer(
    *,
    gis,
    vtpk_path: Path,
    properties: dict[str, Any],
    folder: str,
    source_folder: str,
    folder_obj: Any = None,
    source_folder_obj: Any = None,
    existing_item_id: str | None,
    dataset_id: str,
) -> Any:
    """Publish (or update) a Hosted Vector Tile Service from a pre-built VTPK.

    Rev 3 design (steward-confirmed 2026-05-27): the steward builds
    the ``.vtpk`` manually in ArcGIS Pro's UI ("Share As Vector Tile
    Package"), drops it in ``queue/incoming/``, and ``y2y ingest
    scan`` moves it to ``library/vtpk/<stem>.vtpk``. The push call
    then uploads that file to AGOL and publishes a Vector Tile
    Service from it. **No arcpy in the pipeline runtime, no
    intermediate Feature Service on AGOL.** End state: VTPK source
    item in ``_sources/`` + Vector Tile Service in ``<Category>/``.

    Push refresh detection compares the local VTPK's SHA-256 to a
    ``Y2Y:vtpk_sha256:<hex>`` typeKeyword written onto the AGOL
    source item at the previous push. If they differ (steward
    rebuilt the VTPK and re-ran ``ingest scan``), the source item's
    data is replaced via ``source.update(data=...) +
    source.publish(overwrite=True)``, then the typeKeyword is
    updated. AGOL preserves the VTS item ID through this refresh,
    so the service's URL is stable across source data changes.

    Args:
        gis: Active ``arcgis.gis.GIS`` connection.
        vtpk_path: Local path to the canonical VTPK at
            ``library/vtpk/<stem>.vtpk``. Caller (push()) is
            responsible for verifying the file exists and surfacing
            the actionable "no VTPK ingested" error if it doesn't.
        properties, folder, source_folder, folder_obj, source_folder_obj:
            Standard AGOL push plumbing — see _publish_feature_layer
            for the shared semantics.
        existing_item_id: Catalogue's recorded ``agol_item_id``, or
            ``None`` for a create.
        dataset_id: Used in changelog notes and source-item
            typeKeywords (``Y2Y:dataset_id:<id>``).

    Returns:
        The Vector Tile Service Item.
    """
    item_props = _strip_internal(properties)
    vtpk_sha = _read_vtpk_sidecar_or_compute(vtpk_path)

    if existing_item_id is None:
        # ----- create path -----
        # Source VTPK gets minimal-source-policy props plus a
        # checksum typeKeyword so future pushes can detect "VTPK
        # changed on disk" without re-downloading the package from
        # AGOL.
        source_props = _compute_source_item_properties(
            item_props, dataset_id, source_type="Vector Tile Package",
        )
        source_props.setdefault("typeKeywords", []).append(
            f"Y2Y:vtpk_sha256:{vtpk_sha}"
        )
        # Use Folder.add() rather than the deprecated gis.content.add()
        # to sidestep the arcgis 2.4.x lazy-loader bug that fires
        # 'AttributeError: module arcgis.features.geo has no attribute
        # _is_geoenabled' depending on import order. _ensure_folder
        # returns the live Folder instance; if it failed we fall back
        # to gis.content.add() with the string folder name (best-
        # effort under the same caveat).
        vtpk_item = _add_item_to_folder(
            gis=gis,
            source_props=source_props,
            file_path=vtpk_path,
            source_folder=source_folder,
            source_folder_obj=source_folder_obj,
            properties=properties,
        )
        try:
            service_item = vtpk_item.publish(file_type="Vector Tile Package")
        except Exception as exc:
            raise AgolError(
                f"hosted vector tile publish failed for "
                f"{dataset_id!r}: {exc}"
            ) from exc

        _safe_move(service_item, folder_obj or folder, properties)
        service_item.update(item_properties=item_props)
        return service_item

    # ----- update path -----
    service = _resolve_service_item(gis, existing_item_id)
    source = _find_source_item(service)

    if source is None:
        # No Service2Data link — the VTS was published outside our
        # integration, or the source item was deleted out-of-band.
        # We can't refresh tile data without the source; record a
        # warning and continue with metadata-only reconcile.
        _record_warning(properties, (
            "vector-tile data refresh skipped: no Service2Data link "
            "to a source VTPK. Existing tiles continue serving but "
            "won't reflect any recent VTPK changes. To re-link, "
            "unpublish this row and re-push."
        ))
    else:
        # Compare local VTPK SHA against the one stored on the AGOL
        # source item's typeKeywords. Mismatch → refresh.
        agol_sha = _extract_vtpk_sha_from_keywords(
            getattr(source, "typeKeywords", None) or []
        )
        if agol_sha != vtpk_sha:
            try:
                source.update(data=str(vtpk_path))
                source.publish(
                    file_type="Vector Tile Package", overwrite=True,
                )
            except Exception as exc:
                _record_warning(properties, (
                    f"VTPK re-publish failed; metadata updated but "
                    f"tile cache may be stale. Underlying: {exc}"
                ))

    # Apply / re-apply the full steward-authored metadata to the
    # service. Idempotent.
    service.update(item_properties=item_props)

    # Enforce folder placement on every push (idempotent — same
    # rationale as in _publish_feature_layer's update path).
    _safe_move(service, folder_obj or folder, properties)

    # Reconcile source on every push, refreshing the
    # Y2Y:vtpk_sha256 typeKeyword so future pushes see the current
    # disk checksum.
    if source is not None:
        _reconcile_source_item(
            source, dataset_id,
            source_folder=source_folder,
            source_folder_obj=source_folder_obj,
            source_type="Vector Tile Package",
            service_properties=item_props,
            properties=properties,
            extra_type_keywords=(f"Y2Y:vtpk_sha256:{vtpk_sha}",),
        )
    return service


def _add_item_to_folder(
    *,
    gis,
    source_props: dict[str, Any],
    file_path: Path,
    source_folder: str,
    source_folder_obj: Any,
    properties: dict[str, Any] | None = None,
) -> Any:
    """Add a file as a new item to a folder, preferring Folder.add().

    The legacy ``gis.content.add()`` is deprecated in arcgis 2.3+
    and exhibits a non-deterministic lazy-loader bug in 2.4.x where
    ``arcgis.features.geo._is_geoenabled`` raises AttributeError
    mid-call. The new ``Folder.add()`` API bypasses that path
    entirely.

    The new API takes ``ItemProperties`` (a dataclass) instead of a
    dict, and ``file=`` instead of ``data=``, and returns a ``Job``
    rather than an ``Item``. We translate our existing dict-based
    property shape into ItemProperties and wait on the Job for the
    Item.

    Falls back to the deprecated ``gis.content.add()`` if the
    ``source_folder_obj`` isn't a Folder instance (e.g., the
    ``_ensure_folder`` call earlier returned the string fallback
    because the SDK didn't expose a Folder for that name). The
    fallback may still hit the lazy-loader bug; report failure
    clearly.

    Filename-collision recovery: AGOL refuses uploads when an item
    with the same filename already exists for the user (CONT_0027,
    code 409). This commonly happens when a previous push left a
    source item behind (e.g., a target-switch's Service2Data walk
    missed the source, or a failed publish left an orphan). On
    that error we parse the conflicting item ID, delete it
    permanently, and retry once. ``properties`` is used to record
    a ``[agol]`` warning when this fires so the steward sees a
    note that a stale source was reclaimed.
    """
    from arcgis.gis import ItemProperties, ItemTypeEnum

    # Map our string ``type`` to ItemTypeEnum.
    _TYPE_MAP = {
        "Vector Tile Package": ItemTypeEnum.VECTOR_TILE_PACKAGE,
        "GeoPackage": ItemTypeEnum.GEOPACKAGE,
        "Image": ItemTypeEnum.IMAGE,
    }
    item_type = _TYPE_MAP.get(source_props.get("type", ""))
    if item_type is None:
        # Fallback — pass the string straight through; ItemTypeEnum
        # accepts strings too per its annotation.
        item_type = source_props.get("type") or ""

    ip = ItemProperties(
        title=source_props.get("title") or "",
        item_type=item_type,
        description=source_props.get("description"),
        type_keywords=source_props.get("typeKeywords") or None,
    )

    # Prefer the live Folder instance. If we only have a string,
    # try to resolve the folder via gis.content.folders.get().
    folder = source_folder_obj
    if not hasattr(folder, "add"):
        try:
            folder = gis.content.folders.get(folder=source_folder)
        except Exception:
            folder = None

    if folder is not None and hasattr(folder, "add"):
        try:
            job = folder.add(item_properties=ip, file=str(file_path))
            return job.result()
        except Exception as exc:
            # Detect filename-collision and self-heal by deleting
            # the conflicting item, then retry once.
            conflicting_id = _extract_filename_conflict_id(str(exc))
            if conflicting_id is None:
                raise
            cleaned = _cleanup_conflicting_item(
                gis, conflicting_id, properties,
            )
            if not cleaned:
                raise
            # Retry the upload once. If it fails again let the
            # exception propagate.
            job = folder.add(item_properties=ip, file=str(file_path))
            return job.result()

    # Last-resort fallback to the deprecated API (likely to hit the
    # lazy-loader bug, but we have nothing else).
    return gis.content.add(
        item_properties=source_props,
        data=str(file_path),
        folder=source_folder,
    )


def _extract_filename_conflict_id(message: str) -> str | None:
    """Parse an AGOL filename-collision error for the existing item ID.

    The arcgis SDK raises ``FolderException`` with a message like:

        The item could not be added: {'error': {'code': 409,
        'messageCode': 'CONT_0027', 'message': 'Item with this
        filename already exists. [itemId=11a45f8d0be94d0c98ad1ca9b967a9ed]',
        ...}}

    We don't have a structured payload to introspect, so we regex
    for the ``itemId=<hex>`` token. Returns ``None`` if the message
    isn't a filename-collision error (caller should propagate the
    original exception in that case).
    """
    import re
    if "already exists" not in message:
        return None
    match = re.search(r"itemId=([0-9a-fA-F]+)", message)
    return match.group(1) if match else None


def _cleanup_conflicting_item(
    gis,
    item_id: str,
    properties: dict[str, Any] | None,
) -> bool:
    """Permanently delete a conflicting AGOL item; return success.

    Used by ``_add_item_to_folder``'s filename-collision recovery
    path. Records a ``[agol]`` warning if ``properties`` is
    provided so the steward sees that a stale source was reclaimed
    during this push.

    Returns ``True`` if the item was successfully deleted (and the
    caller can safely retry the upload), ``False`` otherwise.
    """
    try:
        existing = gis.content.get(item_id)
    except Exception:
        return False
    if existing is None:
        # Already gone (maybe AGOL eventual consistency); treat as
        # success so the retry can proceed.
        return True
    try:
        try:
            existing.delete(permanent=True)
        except TypeError:
            existing.delete()
    except Exception as exc:
        if properties is not None:
            _record_warning(properties, (
                f"could not clean up conflicting item {item_id!r} "
                f"during upload: {exc}"
            ))
        return False
    if properties is not None:
        existing_title = getattr(existing, "title", "(unknown)")
        _record_warning(properties, (
            f"replaced stale AGOL item {item_id!r} (title="
            f"{existing_title!r}) that conflicted with the new "
            f"upload by filename. Previous item permanently deleted."
        ))
    return True


def _read_vtpk_sidecar_or_compute(vtpk_path: Path) -> str:
    """Wrapper around ``agol_vtpk.read_vtpk_checksum`` used only here.

    Kept as a local helper so the import stays lazy (agol_vtpk
    pulls in sqlite3 + inventory_manager); agol_sync.py itself
    doesn't need those at module load.
    """
    from . import agol_vtpk
    return agol_vtpk.read_vtpk_checksum(vtpk_path)


def _extract_vtpk_sha_from_keywords(keywords: list[str]) -> str | None:
    """Return the ``Y2Y:vtpk_sha256:<hex>`` value from typeKeywords."""
    prefix = "Y2Y:vtpk_sha256:"
    for kw in keywords:
        if isinstance(kw, str) and kw.startswith(prefix):
            return kw[len(prefix):]
    return None


# --- sharing -----------------------------------------------------------

def _resolve_sharing(
    config: AgolConfig,
    gis,
    sharing_override: str | None,
) -> dict[str, Any]:
    """Compute the sharing payload for ``item.sharing.shared_with``.

    Default: org-visible + shared with the Y2Y Conservation Atlas
    group. The group ID is resolved lazily (cached).

    CLI ``--sharing`` flag values:
        'private' → no sharing (only the owner can see)
        'org'     → org-visible only, no group
        'public'  → everyone, no group
    """
    if sharing_override == "private":
        return {"everyone": False, "org": False, "groups": []}
    if sharing_override == "org":
        return {"everyone": False, "org": True, "groups": []}
    if sharing_override == "public":
        return {"everyone": True, "org": True, "groups": []}
    if sharing_override is not None:
        raise AgolError(
            f"unknown sharing override {sharing_override!r}; "
            f"expected 'private', 'org', or 'public'."
        )

    # Default: org + Conservation Atlas group.
    group_id = resolve_group_id(gis, config)
    return {"everyone": False, "org": True, "groups": [group_id]}


def _apply_sharing(item: Any, payload: dict[str, Any]) -> None:
    """Apply a sharing payload to an item.

    Uses the modern ``item.sharing.sharing_level`` + group-sharing
    API. Falls back to the legacy ``item.share(...)`` call on older
    SDK versions.
    """
    everyone = bool(payload.get("everyone"))
    org = bool(payload.get("org"))
    groups = list(payload.get("groups") or [])

    # Modern path: item.sharing manager.
    sharing = getattr(item, "sharing", None)
    if sharing is not None and hasattr(sharing, "sharing_level"):
        if everyone:
            sharing.sharing_level = "EVERYONE"
        elif org:
            sharing.sharing_level = "ORGANIZATION"
        else:
            sharing.sharing_level = "PRIVATE"
        # Group sharing via the sub-manager.
        groups_mgr = getattr(sharing, "groups", None)
        if groups_mgr is not None and groups:
            for gid in groups:
                try:
                    groups_mgr.add(group=gid)
                except Exception:  # pragma: no cover — defensive
                    # Some SDK versions take an item arg, some don't.
                    # Best-effort: continue if this particular call shape
                    # isn't supported; the org-level sharing still applied.
                    pass
        return

    # Legacy path: item.share(everyone=..., org=..., groups=...).
    try:
        item.share(
            everyone=everyone,
            org=org,
            groups=groups or None,
        )
    except Exception as exc:  # pragma: no cover — defensive
        raise AgolError(
            f"could not apply sharing to item {item.id!r}: {exc}"
        ) from exc


# --- misc helpers ------------------------------------------------------

def _strip_internal(properties: dict[str, Any]) -> dict[str, Any]:
    """Remove any ``_underscore``-prefixed bookkeeping keys before AGOL upload."""
    return {k: v for k, v in properties.items() if not k.startswith("_")}


def _checksum_changed(row: dict[str, Any]) -> bool:
    """True iff the file's checksum has changed since the last sync.

    The catalogue's ``checksum_sha256`` is updated whenever
    ``y2y refresh`` or ingest re-snapshots the file. If the most
    recent recorded checksum differs from what was on disk at the
    last successful push, the AGOL-side data needs replacing.

    For v1 this is approximated by checking whether ``last_synced_at``
    is older than ``date_modified``; we don't store a per-push
    checksum independently. A subtle precision-cost: a metadata-only
    edit (which bumps date_modified) flags as "checksum changed"
    too. That just means we re-upload the source — wasteful but
    safe. v2 could store the last-pushed checksum in a new column.
    """
    last_synced = row.get("last_synced_at")
    if not last_synced:
        return True  # never synced → must upload
    date_modified = row.get("date_modified") or ""
    return date_modified > last_synced


def _format_push_plan(
    *,
    target: str,
    folder: str,
    properties: dict[str, Any],
    sharing_payload: dict[str, Any],
    row: dict[str, Any],
    target_override_used: bool,
) -> str:
    """Render a human-readable dry-run summary."""
    lines = [
        f"target:    {target}{' (CLI override)' if target_override_used else ''}",
        f"folder:    {folder}",
        f"title:     {properties.get('title')}",
        f"category:  {properties.get('categories', [None])[0]}",
        f"tags:      {properties.get('tags')}",
        f"sharing:   org={sharing_payload.get('org')}, "
        f"everyone={sharing_payload.get('everyone')}, "
        f"groups={sharing_payload.get('groups')}",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# push_all_dirty — batch wrapper
# ----------------------------------------------------------------------------

def push_all_dirty(
    db_path: Path,
    gis,
    config: AgolConfig,
    *,
    library_root: Path,
    actor: str,
    sharing_override: str | None = None,
    dry_run: bool = False,
) -> list[SyncResult]:
    """Push every row whose ``sync_status='pending_push'``.

    Per-row failures mark that row's ``sync_status='error'`` (and a
    structured changelog entry captures the failure reason) but do
    not abort the batch — subsequent rows still get a chance.

    Returns the list of ``SyncResult`` for every row attempted (one
    entry per row, in catalogue order).
    """
    from . import inventory_manager
    from .utils import utc_now_iso

    rows = [
        r for r in inventory_manager.load_inventory(db_path)
        if r.get("status") == "active"
        and r.get("sync_status") == "pending_push"
    ]

    results: list[SyncResult] = []
    for row in rows:
        did = row["dataset_id"]
        try:
            result = push(
                db_path, did, gis, config,
                library_root=library_root, actor=actor,
                sharing_override=sharing_override,
                dry_run=dry_run,
            )
        except AgolError as exc:
            # Mark the row as 'error' and record a changelog entry,
            # then continue.
            if not dry_run:
                inventory_manager.update_dataset(
                    db_path, did, {"sync_status": "error"},
                )
                inventory_manager.append_changelog(
                    db_path,
                    timestamp=utc_now_iso(),
                    action="metadata",
                    dataset_id=did,
                    actor=actor,
                    path=row.get("file_path"),
                    detail=f"push failed: {exc}",
                    field_changed="sync_status",
                    old_value="pending_push",
                    new_value="error",
                )
            results.append(SyncResult(
                dataset_id=did,
                action="push" if not dry_run else "push (dry-run)",
                sync_status_before="pending_push",
                sync_status_after="error" if not dry_run else "pending_push",
                agol_item_id=row.get("agol_item_id"),
                note=str(exc),
                error=str(exc),
            ))
        else:
            results.append(result)
    return results


# ----------------------------------------------------------------------------
# Adoption (Phase C)
# ----------------------------------------------------------------------------

# Field-by-field comparison for adoption. Each tuple is
# (catalogue_field, agol_attribute, comparator) where comparator(cat, agol)
# returns True if the values match. Order matters only for the
# reported diff sequence (titles first, then snippet, etc.).
def _tag_set(value: Any) -> set[str]:
    """Normalise a tags value to a set for set-equality comparison.

    Catalogue stores tags as a ``;``-delimited string. AGOL exposes
    ``item.tags`` as a list. Trim/lowercase nothing — case matters
    for AGOL search.
    """
    if value is None:
        return set()
    if isinstance(value, str):
        return {t.strip() for t in value.split(";") if t.strip()}
    if isinstance(value, (list, tuple)):
        return {str(t).strip() for t in value if str(t).strip()}
    return {str(value).strip()}


def _diff_adoption_fields(
    row: dict[str, Any], item: Any,
) -> list[tuple[str, Any, Any]]:
    """Return per-field (field_name, agol_value, catalogue_value) diffs.

    Empty list means the AGOL item matches the catalogue's intended
    state field-for-field. A non-empty list means at least one field
    drifted — adoption marks the row 'conflict' and the steward
    resolves via Phase D pull.

    Diff semantics:

    * Strings: stripped, normalise None → '' so 'no value' on either
      side compares equal to '' on the other.
    * Tags: set-equality (order-independent, whitespace-trimmed).
    * Categories: list-equality after normalising AGOL's list to a
      single canonical display name.
    """
    diffs: list[tuple[str, Any, Any]] = []
    expected = compute_item_properties(row)

    def _str(v: Any) -> str:
        return (v or "").strip() if isinstance(v, (str, type(None))) else str(v).strip()

    # Title
    cat_title = _str(expected.get("title"))
    agol_title = _str(getattr(item, "title", None))
    if cat_title != agol_title:
        diffs.append(("title", agol_title, cat_title))

    # Snippet (summary)
    cat_snippet = _str(expected.get("snippet"))
    agol_snippet = _str(getattr(item, "snippet", None))
    if cat_snippet != agol_snippet:
        diffs.append(("snippet", agol_snippet, cat_snippet))

    # Description
    cat_desc = _str(expected.get("description"))
    agol_desc = _str(getattr(item, "description", None))
    if cat_desc != agol_desc:
        diffs.append(("description", agol_desc, cat_desc))

    # Tags — set-equality.
    cat_tags = _tag_set(expected.get("tags"))
    agol_tags = _tag_set(getattr(item, "tags", None))
    if cat_tags != agol_tags:
        diffs.append(("tags", sorted(agol_tags), sorted(cat_tags)))

    # Access information (acknowledgements)
    cat_access = _str(expected.get("accessInformation"))
    agol_access = _str(getattr(item, "accessInformation", None))
    if cat_access != agol_access:
        diffs.append(("accessInformation", agol_access, cat_access))

    # License info (terms_of_use)
    cat_license = _str(expected.get("licenseInfo"))
    agol_license = _str(getattr(item, "licenseInfo", None))
    if cat_license != agol_license:
        diffs.append(("licenseInfo", agol_license, cat_license))

    # Categories — list-equality after normalising both sides.
    cat_cats = list(expected.get("categories") or [])
    agol_cats = list(getattr(item, "categories", None) or [])
    if sorted(cat_cats) != sorted(agol_cats):
        diffs.append(("categories", agol_cats, cat_cats))

    return diffs


def adopt_row(
    db_path: Path,
    dataset_id: str,
    gis,
    config: AgolConfig,
    *,
    actor: str,
) -> SyncResult:
    """Adopt one row under sync management.

    Pre-condition: the row has ``agol_item_id`` set and
    ``sync_status='unpublished'`` (a "pre-existing AGOL item" that
    was published manually before this integration existed). Post-
    adoption the row is either ``'clean'`` (no drift) or
    ``'conflict'`` (drift; Phase D pull resolves), and is now
    under bidirectional sync management.

    Adoption never mutates AGOL. It only reads the AGOL item and
    updates the catalogue's sync-state columns + writes a
    changelog entry. Steward decides direction at conflict
    resolution via ``y2y agol-sync pull <id>``.

    Raises ``AgolError`` if the row isn't in adoption-eligible
    state, or if the AGOL connection fails outright (Item-missing
    is handled in-band by marking sync_status='error').
    """
    row = inventory_manager.get_dataset(db_path, dataset_id)
    if row is None:
        raise AgolError(f"row {dataset_id!r} not in catalogue")
    sync_status_before = row.get("sync_status") or "unpublished"
    agol_item_id = row.get("agol_item_id")
    if not agol_item_id:
        raise AgolError(
            f"row {dataset_id!r} has no agol_item_id; nothing to adopt"
        )
    if sync_status_before != "unpublished":
        raise AgolError(
            f"row {dataset_id!r} sync_status is {sync_status_before!r}; "
            f"only 'unpublished' rows are eligible for adoption"
        )

    # Fetch the AGOL item. ``gis.content.get()`` returns ``None`` if
    # the item doesn't exist on AGOL.
    item = gis.content.get(agol_item_id)
    now = utc_now_iso()
    if item is None:
        # AGOL item is gone (deleted out-of-band). Mark error so the
        # steward can decide: re-push from catalogue or unpublish.
        msg = (
            f"AGOL item {agol_item_id!r} no longer exists "
            f"(deleted out-of-band?). Use `y2y agol-sync unpublish "
            f"{dataset_id}` to clear the catalogue link, or "
            f"`y2y agol-sync push {dataset_id}` to re-create."
        )
        inventory_manager.update_dataset(
            db_path, dataset_id,
            {"sync_status": "error",
             "internal_notes": _append_note(row.get("internal_notes"), f"[agol] adoption: {msg}")},
        )
        inventory_manager.append_changelog(
            db_path,
            timestamp=now,
            action="metadata",
            dataset_id=dataset_id,
            actor=actor,
            path=row.get("file_path"),
            field_changed="sync_status",
            old_value="unpublished",
            new_value="error",
            detail=f"adoption failed: {msg}",
        )
        return SyncResult(
            dataset_id=dataset_id,
            action="adopt",
            sync_status_before=sync_status_before,
            sync_status_after="error",
            agol_item_id=agol_item_id,
            note=msg,
            error=msg,
        )

    diffs = _diff_adoption_fields(row, item)
    # AGOL-side "created" timestamp gives us a reasonable
    # agol_published_at. Falls back to now if not exposed.
    agol_published_at = (
        getattr(item, "created", None) or now
    )
    # ``created`` on arcgis SDK Items is a Unix epoch millisecond
    # integer. Convert to ISO-8601 Z form if needed.
    if isinstance(agol_published_at, (int, float)):
        from datetime import datetime, timezone
        agol_published_at = datetime.fromtimestamp(
            agol_published_at / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not diffs:
        # Clean adoption — AGOL matches catalogue.
        inventory_manager.update_dataset(
            db_path, dataset_id,
            {
                "sync_status": "clean",
                "last_synced_at": now,
                # Only set agol_published_at if not already populated;
                # don't overwrite a prior value (e.g. from a past push).
                **({"agol_published_at": agol_published_at}
                   if not row.get("agol_published_at") else {}),
            },
        )
        inventory_manager.append_changelog(
            db_path,
            timestamp=now,
            action="metadata",
            dataset_id=dataset_id,
            actor=actor,
            path=row.get("file_path"),
            field_changed="sync_status",
            old_value="unpublished",
            new_value="clean",
            detail=(
                f"adopted: AGOL item {agol_item_id!r} matches catalogue "
                f"field-for-field. Now under sync management."
            ),
        )
        return SyncResult(
            dataset_id=dataset_id,
            action="adopt",
            sync_status_before=sync_status_before,
            sync_status_after="clean",
            agol_item_id=agol_item_id,
            note=f"adopted (no drift)",
        )

    # Conflict — AGOL drifted from catalogue. Steward resolves via
    # Phase D pull. Diff summary lands in changelog as a structured
    # multi-line note so the diff is recoverable.
    diff_lines = [
        f"  {field}: AGOL={agol_val!r}  catalogue={cat_val!r}"
        for field, agol_val, cat_val in diffs
    ]
    diff_summary = "\n".join(diff_lines)
    detail = (
        f"adopted with conflict: AGOL item {agol_item_id!r} has "
        f"{len(diffs)} field(s) that disagree with the catalogue. "
        f"Steward resolves via `y2y agol-sync pull {dataset_id}` "
        f"(Phase D). Diff:\n{diff_summary}"
    )
    inventory_manager.update_dataset(
        db_path, dataset_id,
        {
            "sync_status": "conflict",
            "last_synced_at": now,
            **({"agol_published_at": agol_published_at}
               if not row.get("agol_published_at") else {}),
        },
    )
    inventory_manager.append_changelog(
        db_path,
        timestamp=now,
        action="metadata",
        dataset_id=dataset_id,
        actor=actor,
        path=row.get("file_path"),
        field_changed="sync_status",
        old_value="unpublished",
        new_value="conflict",
        detail=detail,
    )
    return SyncResult(
        dataset_id=dataset_id,
        action="adopt",
        sync_status_before=sync_status_before,
        sync_status_after="conflict",
        agol_item_id=agol_item_id,
        note=f"adopted with conflict ({len(diffs)} field(s) drifted)",
    )


def _append_note(prior: str | None, addition: str) -> str:
    """Append ``addition`` to ``prior`` (or use ``addition`` if empty)."""
    prior = (prior or "").strip()
    if not prior:
        return addition
    return f"{prior}\n{addition}"
