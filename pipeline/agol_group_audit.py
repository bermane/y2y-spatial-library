"""Two-way audit between the catalogue and the Conservation Atlas group.

The Y2Y Conservation Atlas group is the ArcGIS Online home of the
spatial library, so its contents and the catalogue should agree. This
module lists the group once and compares it with the catalogue in
both directions:

* **Group → catalogue.** Layers shared to the group that no catalogue
  row links to (someone added a layer by hand, or by URL from an
  outside provider), layers left behind by the pipeline, and layers
  whose dataset has since been deprecated or tombstoned.
* **Catalogue → group.** Active datasets that have an ``agol_item_id``
  but are not shared to the group, or whose AGOL item no longer
  exists.

The audit is **read-only**. It never changes the catalogue and never
changes anything on AGOL; it only reports. It runs as part of the
weekly ``y2y agol-sync reconcile`` (see
:func:`pipeline.agol_sync.reconcile_bidirectional`), which appends the
findings to its markdown report. See DESIGN.md §15, "Group audit".

Scope is that one group. The rest of the org's content is never
scanned.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlparse

from .agol_config import AgolConfig

# Upper bound on the group listing. Far above any realistic size for
# the Atlas group; the report says so if the listing hits it.
MAX_GROUP_ITEMS = 10000

# AGOL item types that are map layers (data a map can draw). Anything
# else in the group (web maps, apps, dashboards, documents) is counted
# by type but not flagged: it is not data the library could hold.
LAYER_ITEM_TYPES: frozenset[str] = frozenset({
    "Feature Service",
    "Feature Collection",
    "Image Service",
    "Vector Tile Service",
    "Map Service",
    "Scene Service",
    "WMS",
    "WMTS",
    "WFS",
    "KML",
    "KML Collection",
    "OGCFeatureServer",
    "GeoJson",
    "Stream Service",
})

_DATASET_KEYWORD_PREFIX = "Y2Y:dataset_id:"

# Hosts that serve content stored in ArcGIS Online itself. A layer
# whose URL is on any other host points at an outside provider.
_AGOL_HOST_SUFFIXES: tuple[str, ...] = (".arcgis.com", ".arcgisonline.com")


class GroupItem(NamedTuple):
    """One item shared to the group that the audit lists."""

    item_id: str
    title: str
    item_type: str
    owner: str
    url: str
    modified: str          # ISO date, or '' when unknown
    license_info: bool     # True when the item has terms-of-use text
    dataset_id: str        # catalogue row or stamped keyword; '' if none
    note: str


class CatalogueFinding(NamedTuple):
    """One active catalogue dataset that fails the group check."""

    dataset_id: str
    title: str
    agol_item_id: str
    note: str


class GroupAudit(NamedTuple):
    """Result of one :func:`audit_group` run."""

    group_name: str
    error: str | None               # set when the audit could not run
    group_item_count: int
    tracked_count: int
    truncated: bool                 # listing hit MAX_GROUP_ITEMS
    should_be_gone: list[GroupItem]
    pipeline_orphans: list[GroupItem]
    untracked_y2y: list[GroupItem]
    untracked_outside: list[GroupItem]
    non_layer_counts: dict[str, int]
    not_in_group: list[CatalogueFinding]
    missing_on_agol: list[CatalogueFinding]
    unchecked: list[CatalogueFinding]

    @property
    def findings_count(self) -> int:
        """Number of things that need the steward's attention."""
        return (
            len(self.should_be_gone) + len(self.pipeline_orphans)
            + len(self.untracked_y2y) + len(self.untracked_outside)
            + len(self.not_in_group) + len(self.missing_on_agol)
        )


def failed_audit(group_name: str, error: str) -> GroupAudit:
    """A :class:`GroupAudit` recording that the audit could not run."""
    return GroupAudit(
        group_name=group_name, error=error,
        group_item_count=0, tracked_count=0, truncated=False,
        should_be_gone=[], pipeline_orphans=[],
        untracked_y2y=[], untracked_outside=[],
        non_layer_counts={},
        not_in_group=[], missing_on_agol=[], unchecked=[],
    )


# ----------------------------------------------------------------------------
# Reading AGOL items defensively
# ----------------------------------------------------------------------------

def _text(item: Any, name: str) -> str:
    """Return a string attribute of an AGOL item, or '' if absent."""
    value = getattr(item, name, None)
    if not isinstance(value, str) and isinstance(item, dict):
        value = item.get(name)
    return value if isinstance(value, str) else ""


def _keywords(item: Any) -> list[str]:
    value = getattr(item, "typeKeywords", None)
    if not isinstance(value, (list, tuple)) and isinstance(item, dict):
        value = item.get("typeKeywords")
    if not isinstance(value, (list, tuple)):
        return []
    return [k for k in value if isinstance(k, str)]


def _modified_date(item: Any) -> str:
    """AGOL ``modified`` (Unix ms) as ``YYYY-MM-DD``; '' if unusable."""
    raw = getattr(item, "modified", None)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return ""
    from datetime import UTC, datetime
    try:
        dt = datetime.fromtimestamp(float(raw) / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return ""
    return dt.strftime("%Y-%m-%d")


def _stamped_dataset_id(keywords: list[str]) -> str:
    for kw in keywords:
        if kw.startswith(_DATASET_KEYWORD_PREFIX):
            return kw[len(_DATASET_KEYWORD_PREFIX):]
    return ""


def _org_id(gis: Any) -> str:
    """The signed-in org's ID, or '' when it can't be read."""
    try:
        value = gis.properties.id
    except Exception:  # noqa: BLE001 - any SDK failure means "unknown"
        return ""
    return value if isinstance(value, str) else ""


def _is_outside_provider(item: Any, org_id: str) -> bool:
    """True when a layer's data lives outside this AGOL org.

    Two signals: the item belongs to another organisation, or its URL
    is on a server that is not ArcGIS Online (for example a provincial
    government map server registered "from URL").
    """
    item_org = _text(item, "orgId")
    if org_id and item_org and item_org != org_id:
        return True
    url = _text(item, "url")
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return not host.endswith(_AGOL_HOST_SUFFIXES)


def _group_item(item: Any, dataset_id: str = "", note: str = "") -> GroupItem:
    return GroupItem(
        item_id=_text(item, "id"),
        title=_text(item, "title") or "(untitled)",
        item_type=_text(item, "type") or "(unknown type)",
        owner=_text(item, "owner"),
        url=_text(item, "url"),
        modified=_modified_date(item),
        license_info=bool(_text(item, "licenseInfo").strip()),
        dataset_id=dataset_id,
        note=note,
    )


def _list_group_items(gis: Any, group_id: str) -> list[Any]:
    """Every item shared to the group, including outside-owned ones."""
    group = gis.groups.get(group_id)
    if group is None:
        raise RuntimeError(f"group {group_id!r} not found")
    content = group.content(max_items=MAX_GROUP_ITEMS)
    if not isinstance(content, (list, tuple)):
        raise TypeError("group listing did not return a list of items")
    return list(content)


def _finding(row: dict[str, Any], note: str) -> CatalogueFinding:
    return CatalogueFinding(
        dataset_id=row["dataset_id"],
        title=row.get("title") or "",
        agol_item_id=row.get("agol_item_id") or "",
        note=note,
    )


# ----------------------------------------------------------------------------
# The audit
# ----------------------------------------------------------------------------

def audit_group(db_path: Path, gis: Any, config: AgolConfig) -> GroupAudit:
    """Compare the Conservation Atlas group with the catalogue.

    Read-only on both sides. Never raises: if AGOL can't be reached or
    the listing fails, the returned audit carries ``error`` and empty
    findings, so the calling reconcile still completes.
    """
    from . import agol_sync, inventory_manager

    group_name = config.conservation_atlas_group_name
    try:
        group_id = agol_sync.resolve_group_id(gis, config)
        items = _list_group_items(gis, group_id)
    except Exception as exc:  # noqa: BLE001 - the audit must never raise
        return failed_audit(
            group_name,
            f"could not list the group ({exc.__class__.__name__}: {exc})",
        )

    rows = inventory_manager.load_inventory(db_path)
    row_by_item_id: dict[str, dict[str, Any]] = {
        r["agol_item_id"]: r for r in rows if r.get("agol_item_id")
    }
    org_id = _org_id(gis)

    should_be_gone: list[GroupItem] = []
    pipeline_orphans: list[GroupItem] = []
    untracked_y2y: list[GroupItem] = []
    untracked_outside: list[GroupItem] = []
    non_layers: Counter[str] = Counter()
    group_item_ids: set[str] = set()
    tracked = 0

    for item in items:
        item_id = _text(item, "id")
        if not item_id:
            continue
        group_item_ids.add(item_id)
        keywords = _keywords(item)
        item_type = _text(item, "type") or "(unknown type)"

        row = row_by_item_id.get(item_id)
        if row is not None:
            status = row.get("status") or "active"
            if status == "active":
                tracked += 1
            else:
                should_be_gone.append(_group_item(
                    item, row["dataset_id"], f"dataset is {status}",
                ))
            continue

        stamped = _stamped_dataset_id(keywords)
        if stamped or "Y2Y" in keywords:
            pipeline_orphans.append(_group_item(
                item, stamped,
                "published by the pipeline, but no catalogue dataset "
                "links to it",
            ))
            continue

        if item_type not in LAYER_ITEM_TYPES:
            non_layers[item_type] += 1
            continue

        if _is_outside_provider(item, org_id):
            untracked_outside.append(_group_item(item))
        else:
            untracked_y2y.append(_group_item(item))

    # ----- catalogue → group -------------------------------------------
    not_in_group: list[CatalogueFinding] = []
    missing_on_agol: list[CatalogueFinding] = []
    unchecked: list[CatalogueFinding] = []
    truncated = len(items) >= MAX_GROUP_ITEMS

    for row in rows:
        if (row.get("status") or "active") != "active":
            continue
        agol_item_id = row.get("agol_item_id")
        if not agol_item_id or agol_item_id in group_item_ids:
            continue

        try:
            item = gis.content.get(agol_item_id)
        except Exception as exc:  # noqa: BLE001 - report it, keep auditing
            unchecked.append(_finding(
                row, f"AGOL contact failed: {exc.__class__.__name__}",
            ))
            continue
        if item is None:
            missing_on_agol.append(_finding(
                row, "the AGOL item no longer exists",
            ))
        else:
            access = _text(item, "access") or "unknown"
            not_in_group.append(_finding(row, f"current sharing: {access}"))

    def _by_title(entries: list[GroupItem]) -> list[GroupItem]:
        return sorted(entries, key=lambda e: (e.title.lower(), e.item_id))

    return GroupAudit(
        group_name=group_name,
        error=None,
        group_item_count=len(group_item_ids),
        tracked_count=tracked,
        truncated=truncated,
        should_be_gone=_by_title(should_be_gone),
        pipeline_orphans=_by_title(pipeline_orphans),
        untracked_y2y=_by_title(untracked_y2y),
        untracked_outside=_by_title(untracked_outside),
        non_layer_counts=dict(sorted(non_layers.items())),
        not_in_group=not_in_group,
        missing_on_agol=missing_on_agol,
        unchecked=unchecked,
    )


# ----------------------------------------------------------------------------
# Markdown rendering (appended to the weekly reconcile report)
# ----------------------------------------------------------------------------

def _item_line(entry: GroupItem, *, show_license: bool = False) -> str:
    parts = [f"- `{entry.item_id}` {entry.title!r} ({entry.item_type})"]
    details: list[str] = []
    if entry.dataset_id:
        details.append(f"dataset `{entry.dataset_id}`")
    if entry.owner:
        details.append(f"owner {entry.owner}")
    if entry.modified:
        details.append(f"modified {entry.modified}")
    if entry.url:
        details.append(entry.url)
    if show_license:
        details.append(
            "terms of use present" if entry.license_info
            else "NO terms of use on the item"
        )
    if entry.note:
        details.append(entry.note)
    if details:
        parts.append(" — " + "; ".join(details))
    return "".join(parts)


def _finding_line(finding: CatalogueFinding) -> str:
    return (
        f"- `{finding.dataset_id}` {finding.title!r} — AGOL item "
        f"`{finding.agol_item_id}`: {finding.note}"
    )


def render_markdown(audit: GroupAudit) -> list[str]:
    """Render the audit as markdown lines for the reconcile report."""
    lines: list[str] = [f"## Group audit: {audit.group_name}", ""]

    if audit.error:
        lines.extend([
            (f"_The group audit did not run: {audit.error}. Nothing else "
            f"in this report is affected. It will be tried again at the "
            f"next reconcile._"),
            "",
        ])
        return lines

    lines.extend([
        ("Read-only comparison of the group with the catalogue. Nothing "
        "below was changed."),
        "",
        f"- {audit.group_item_count:3d}  items shared to the group",
        f"- {audit.tracked_count:3d}  tracked by an active catalogue dataset",
        f"- {len(audit.untracked_y2y):3d}  untracked layers hosted by Y2Y",
        (f"- {len(audit.untracked_outside):3d}  untracked layers from "
        f"outside providers"),
        (f"- {len(audit.pipeline_orphans):3d}  pipeline-published layers "
        f"with no catalogue dataset"),
        (f"- {len(audit.should_be_gone):3d}  layers of deprecated or "
        f"tombstoned datasets"),
        (f"- {sum(audit.non_layer_counts.values()):3d}  items that are not "
        f"layers (not flagged)"),
        (f"- {len(audit.not_in_group):3d}  catalogue datasets on AGOL but "
        f"not in the group"),
        (f"- {len(audit.missing_on_agol):3d}  catalogue datasets whose AGOL "
        f"item is gone"),
        "",
    ])
    if audit.truncated:
        lines.extend([
            (f"_Warning: the group listing reached the {MAX_GROUP_ITEMS}-item "
            f"limit, so this audit may be incomplete._"),
            "",
        ])
    if audit.findings_count == 0 and not audit.unchecked:
        lines.extend([
            "_The group and the catalogue agree. Nothing to review._",
            "",
        ])

    if audit.not_in_group:
        lines.extend([
            "### Catalogue datasets on AGOL but not in the group",
            "",
            ("These should always be shared to the group. Share the item "
            "to the group on AGOL, or re-run `y2y agol-sync push <id>`."),
            "",
            *[_finding_line(f) for f in audit.not_in_group],
            "",
        ])
    if audit.missing_on_agol:
        lines.extend([
            "### Catalogue datasets whose AGOL item is gone",
            "",
            ("The item was deleted on AGOL outside the pipeline. Run "
            "`y2y agol-sync unpublish <id>` to clear the link, then "
            "`y2y agol-sync push <id>` to publish again if wanted."),
            "",
            *[_finding_line(f) for f in audit.missing_on_agol],
            "",
        ])
    if audit.untracked_y2y:
        lines.extend([
            "### Untracked layers hosted by Y2Y",
            "",
            ("In the group, but not in the catalogue. Decide for each: "
            "ingest it into the library, link it to an existing dataset, "
            "or remove it from the group."),
            "",
            *[_item_line(e) for e in audit.untracked_y2y],
            "",
        ])
    if audit.untracked_outside:
        lines.extend([
            "### Untracked layers from outside providers",
            "",
            ("In the group, but the data lives on another organisation's "
            "server. Check the terms of use before copying any of these "
            "into the library."),
            "",
            *[_item_line(e, show_license=True)
              for e in audit.untracked_outside],
            "",
        ])
    if audit.pipeline_orphans:
        lines.extend([
            "### Pipeline-published layers with no catalogue dataset",
            "",
            ("These carry the pipeline's `Y2Y` keywords, but no catalogue "
            "dataset links to them (for example, layers published from "
            "an earlier catalogue). Relink or remove each one."),
            "",
            *[_item_line(e) for e in audit.pipeline_orphans],
            "",
        ])
    if audit.should_be_gone:
        lines.extend([
            "### Layers of deprecated or tombstoned datasets",
            "",
            ("Still shared to the group although the dataset is no longer "
            "active. Reconcile removes these layers, so one listed here "
            "could not be deleted (see \"Removal from AGOL failed\" "
            "above) or this was a dry run."),
            "",
            *[_item_line(e) for e in audit.should_be_gone],
            "",
        ])
    if audit.unchecked:
        lines.extend([
            "### Could not be checked",
            "",
            *[_finding_line(f) for f in audit.unchecked],
            "",
        ])
    if audit.non_layer_counts:
        lines.extend([
            "### Items that are not layers (counted only)",
            "",
            *[f"- {n:3d}  {item_type}"
              for item_type, n in audit.non_layer_counts.items()],
            "",
        ])
    return lines
