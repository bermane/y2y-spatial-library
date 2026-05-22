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
# 'conflict' must be resolved first (Phase D), and 'clean' rows don't
# need pushing (the catalogue hasn't moved).
_PUSHABLE_STATUSES: frozenset[str] = frozenset({
    "unpublished", "pending_push", "error",
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
    ``row['agol_target']``), branches by target type, generates a
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
        target_override: If set, overrides the row's ``agol_target``
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

    target = target_override or row.get("agol_target")
    if target not in _VALID_TARGET_FORMAT:
        raise AgolError(
            f"unknown agol_target {target!r} on {dataset_id!r}. "
            f"Expected one of: {sorted(_VALID_TARGET_FORMAT)}"
        )
    source_format = row.get("format")
    allowed_formats = _VALID_TARGET_FORMAT[target]
    if source_format not in allowed_formats:
        raise AgolError(
            f"agol_target {target!r} is not valid for format "
            f"{source_format!r} (allowed: {sorted(allowed_formats)})."
        )

    # ----- compute the AGOL-side payload -------------------------------
    properties = compute_item_properties(row)
    folder = compute_target_folder(
        row["category"], prefix=config.folder_prefix
    )
    sharing_payload = _resolve_sharing(config, gis, sharing_override)

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
        properties["_thumb_warning"] = f"thumbnail generation failed: {exc}"

    # ----- per-target publish path -------------------------------------
    source_path = library_root / row["file_path"]
    if not source_path.exists():
        raise AgolError(
            f"source file missing for {dataset_id!r}: {source_path}"
        )

    if target == "feature-layer":
        item = _publish_feature_layer(
            gis=gis, source_path=source_path,
            properties=properties, folder=folder,
            existing_item_id=row.get("agol_item_id"),
            checksum_changed=_checksum_changed(row),
        )
    elif target == "vector-tile-layer":
        item = _publish_vector_tile_layer(
            gis=gis, source_path=source_path,
            properties=properties, folder=folder,
            existing_item_id=row.get("agol_item_id"),
            dataset_id=dataset_id, checksum=row["checksum_sha256"],
            cache_dir=cache_dir,
            checksum_changed=_checksum_changed(row),
        )
    elif target == "imagery-layer":
        item = _publish_imagery_layer(
            gis=gis, source_path=source_path,
            properties=properties, folder=folder,
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
            properties["_thumb_warning"] = (
                f"thumbnail upload failed post-publish: {exc}"
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

    # If the push fired a thumbnail or fallback warning, surface it in
    # internal_notes so the steward can see something happened.
    warning = properties.pop("_thumb_warning", None)
    fallback = properties.pop("_fallback_warning", None)
    annotation_parts: list[str] = []
    if warning:
        annotation_parts.append(f"[agol] {warning}")
    if fallback:
        annotation_parts.append(f"[agol] {fallback}")
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
        # The source GPKG item carries the same metadata as the
        # eventual service; both get the title, tags, categories, etc.
        # so the steward sees a consistent labelling in either item.
        gpkg_props = dict(item_props)
        gpkg_props.setdefault("type", "GeoPackage")
        gpkg_item = gis.content.add(
            item_properties=gpkg_props,
            data=str(source_path),
            folder=folder,
        )
        try:
            service_item = gpkg_item.publish(file_type="GeoPackage")
        except Exception as exc:
            properties["_fallback_warning"] = (
                f"feature-layer publish failed; item retained as "
                f"downloadable GeoPackage instead. Underlying: {exc}"
            )
            return gpkg_item
        # Re-apply metadata to the service explicitly: ``publish()``
        # doesn't always copy item_properties from the source.
        service_item.update(item_properties=item_props)
        return service_item

    # ----- update path -----
    service = _resolve_service_item(gis, existing_item_id)
    # Metadata is updated on the service (that's what users see).
    service.update(item_properties=item_props)

    if checksum_changed:
        # Data refresh via the FeatureLayerCollection manager. This
        # replaces the data backing the existing service without
        # creating any new items.
        try:
            from arcgis.features import FeatureLayerCollection
            flc = FeatureLayerCollection.fromitem(service)
            flc.manager.overwrite(str(source_path))
        except Exception as exc:
            properties["_fallback_warning"] = (
                f"FeatureLayerCollection.overwrite failed; metadata "
                f"updated but the service's data may be stale. "
                f"Underlying: {exc}"
            )
    return service


def _publish_imagery_layer(
    *,
    gis,
    source_path: Path,
    properties: dict[str, Any],
    folder: str,
    existing_item_id: str | None,
    checksum_changed: bool,
) -> Any:
    """Publish (or update) a Hosted Imagery Layer from a COG source.

    Hosted imagery publishing for COGs is beta in the arcgis SDK. On
    publish failure we fall back to retaining the uploaded GeoTIFF as
    a downloadable item (item-with-attached-file mode) and surface
    that fact in the changelog.

    Returns the **service** item if hosted publish succeeded, or the
    source GeoTIFF item if the fallback fired.
    """
    item_props = _strip_internal(properties)

    if existing_item_id is None:
        # Create path.
        tif_props = dict(item_props)
        tif_props.setdefault("type", "Image")
        tif_item = gis.content.add(
            item_properties=tif_props,
            data=str(source_path),
            folder=folder,
        )
        try:
            service_item = tif_item.publish()
        except Exception as exc:
            properties["_fallback_warning"] = (
                f"hosted imagery publish failed (likely COG beta "
                f"limitation); item retained as downloadable GeoTIFF. "
                f"Underlying: {exc}"
            )
            return tif_item
        service_item.update(item_properties=item_props)
        return service_item

    # Update path.
    service = _resolve_service_item(gis, existing_item_id)
    service.update(item_properties=item_props)

    if checksum_changed:
        # Data refresh: find the source GeoTIFF item via the
        # Service2Data relationship and re-publish through it. AGOL's
        # imagery overwrite path isn't as cleanly exposed as the
        # feature-layer FLC manager; the source-publish approach
        # remains the standard pattern.
        source = _find_source_item(service)
        if source is None:
            properties["_fallback_warning"] = (
                "imagery data refresh skipped: no Service2Data link "
                "back to a source GeoTIFF. Re-publish manually from "
                "the AGOL UI."
            )
        else:
            try:
                source.update(data=str(source_path))
                source.publish(overwrite=True)
            except Exception as exc:
                properties["_fallback_warning"] = (
                    f"imagery re-publish failed; metadata updated but "
                    f"service may be stale. Underlying: {exc}"
                )
    return service


def _publish_vector_tile_layer(
    *,
    gis,
    source_path: Path,
    properties: dict[str, Any],
    folder: str,
    existing_item_id: str | None,
    dataset_id: str,
    checksum: str,
    cache_dir: Path,
    checksum_changed: bool,
) -> Any:
    """Publish (or update) a Hosted Vector Tile Service from a locally-built VTPK.

    Steward-confirmed design (DESIGN.md §15 + plan): we never let AGOL
    create an intermediate Hosted Feature Layer. Instead, we use
    arcpy on the local machine to build a VTPK, upload that VTPK as
    an item, and publish the VTPK item to a Hosted Vector Tile
    Service.

    The arcpy import is detected lazily inside
    ``agol_vtpk.build_vtpk``; missing-arcpy errors surface as
    ``AgolToolingError``.

    Returns the **service** item (Vector Tile Service).
    """
    from . import agol_vtpk

    vtpk_path = agol_vtpk.build_vtpk(
        gpkg_path=source_path,
        dataset_id=dataset_id,
        checksum=checksum,
        cache_dir=cache_dir,
    )

    item_props = _strip_internal(properties)

    if existing_item_id is None:
        # Create path: upload VTPK + publish.
        vtpk_props = dict(item_props)
        vtpk_props.setdefault("type", "Vector Tile Package")
        vtpk_item = gis.content.add(
            item_properties=vtpk_props,
            data=str(vtpk_path),
            folder=folder,
        )
        service_item = vtpk_item.publish(file_type="Vector Tile Package")
        service_item.update(item_properties=item_props)
        return service_item

    # Update path: metadata on the service; data refresh through the
    # source VTPK if the underlying source changed.
    service = _resolve_service_item(gis, existing_item_id)
    service.update(item_properties=item_props)

    if checksum_changed:
        source = _find_source_item(service)
        if source is None:
            properties["_fallback_warning"] = (
                "vector-tile data refresh skipped: no Service2Data "
                "link back to a source VTPK. Re-publish manually."
            )
        else:
            try:
                source.update(data=str(vtpk_path))
                source.publish(
                    file_type="Vector Tile Package", overwrite=True,
                )
            except Exception as exc:
                properties["_fallback_warning"] = (
                    f"VTPK re-publish failed; metadata updated but "
                    f"service may be stale. Underlying: {exc}"
                )
    return service


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
