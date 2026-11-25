"""Default symbology for layers published to ArcGIS Online.

Push writes a standard default style to a layer the **first time** it
is published, so the steward does not have to style every layer by
hand (maintainer decision 2026-11-25). The rules:

* Styling that arrives with the data is kept. A raster's colour table
  and a vector tile package the steward styled by attribute in ArcGIS
  Pro are used as they are. The standards below only fill the gap.
* Vector layers (feature layers, and single-symbol vector tile
  packages): polygons get an outline and no fill, lines a line,
  points a dot, all in the colour of the dataset's library category.
* Continuous rasters: a viridis colour ramp, 30% transparent.
* Categorical rasters: the file's own colour table if it has one,
  otherwise a colour-blind-friendly discrete palette; 30% transparent.

The style is never read back, compared or re-applied. It is not part
of drift detection or reconcile, and there is no catalogue column for
it, so a style customised by hand on AGOL always sticks. The one way
to overwrite a style later is the explicit ``y2y agol-sync restyle``
command (:func:`restyle`), which saves the style it replaces first.

Everything here is best-effort: a failure becomes an ``[agol]``
warning and never blocks a push or a catalogue write.

Review note for 0.9.1: the colours and defaults in this file are a
first version. They are to be checked (including a colour-blindness
simulation) and reviewed with the data steward, then adjusted here.
A changed value only affects layers published, or restyled, after it.

Where the style is stored on AGOL:

* Feature layer: the item's data JSON, ``layers[n].layerDefinition
  .drawingInfo.renderer``. This is what Map Viewer writes on "Save
  layer", and it survives a data refresh.
* Imagery layer: the item's data JSON, ``layerDefinition.drawingInfo
  .renderer`` plus ``opacity`` (web map specification,
  imageServiceLayer). Not yet confirmed against a live hosted imagery
  layer; see DESIGN.md.
* Vector tile layer: the item resource ``styles/root.json``, which
  takes priority over the style inside the package. The ``.vtpk`` in
  the library is never modified.
"""
from __future__ import annotations

import copy
import json
import zipfile
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# The standards. Edit colours and sizes here.
# ---------------------------------------------------------------------------

#: Outline / line / point colour per library category (hex RGB).
CATEGORY_COLOURS: dict[str, str] = {
    "Boundaries, Tenure & Governance": "#333333",      # charcoal
    "Biodiversity & Ecosystems": "#117733",            # green
    "Climate Change": "#E69F00",                       # amber
    "Connectivity & Wildlife Movement": "#AA4499",     # purple
    "Species": "#994F00",                              # brown
    "Water": "#0072B2",                                # blue
    "Land Cover, Land Use & Disturbance": "#999933",   # olive
    "Human Dimensions of Conservation": "#332288",     # indigo
    "Threats & Infrastructure": "#CC3311",             # red
    "Demographics & Socioeconomic Data": "#CC6677",    # rose
}

#: Used when a category has no entry above (should not happen).
FALLBACK_COLOUR = "#4D4D4D"

LINE_WIDTH_PT = 1.5
POINT_SIZE_PT = 7.0
POINT_OUTLINE_WIDTH_PT = 0.75

#: Rasters are drawn 30% transparent, i.e. 70% opaque.
RASTER_OPACITY = 0.7

#: Continuous rasters are stretched between these percentiles.
STRETCH_PERCENTILES = (2.0, 98.0)

#: Viridis, dark purple through green to yellow (five stops).
VIRIDIS_STOPS: tuple[str, ...] = (
    "#440154", "#3B528B", "#21918C", "#5EC962", "#FDE725",
)

#: Okabe-Ito: eight colours that stay distinct for colour-blind viewers.
OKABE_ITO: tuple[str, ...] = (
    "#E69F00", "#56B4E9", "#009E73", "#F0E442",
    "#0072B2", "#D55E00", "#CC79A7", "#000000",
)

#: Twelve more colours for rasters with 9 to 20 classes. Past eight
#: classes no palette is fully colour-blind safe; push says so.
EXTENDED_COLOURS: tuple[str, ...] = (
    "#332288", "#88CCEE", "#44AA99", "#117733", "#999933", "#DDCC77",
    "#CC6677", "#882255", "#AA4499", "#777777", "#994F00", "#BBBBBB",
)

COLOUR_BLIND_SAFE_CLASSES = len(OKABE_ITO)
MAX_PALETTE_CLASSES = len(OKABE_ITO) + len(EXTENDED_COLOURS)

#: Above this many pixels, class values are read from a reduced copy
#: of the raster instead of every pixel.
_FULL_SCAN_MAX_PIXELS = 2_000_000_000
_SAMPLE_MAX_DIM = 2048

_VTPK_STYLE_SUFFIX = "resources/styles/root.json"

Warn = Callable[[str], None]


class SymbologyError(Exception):
    """A default style could not be built or written."""


class NoStandardStyle(SymbologyError):
    """The layer keeps its own styling; there is no standard to apply.

    Raised for a vector tile package styled by attribute in ArcGIS Pro
    and for a categorical raster with too many classes. Not a failure.
    """


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

def hex_to_rgb(value: str) -> tuple[int, int, int]:
    """``"#0072B2"`` -> ``(0, 114, 178)``."""
    text = value.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"not a 6-digit hex colour: {value!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def category_colour(category: str | None) -> tuple[int, int, int]:
    """Return the RGB colour for a library category."""
    return hex_to_rgb(CATEGORY_COLOURS.get(category or "", FALLBACK_COLOUR))


def _rgba(rgb: tuple[int, int, int], alpha: int = 255) -> list[int]:
    return [rgb[0], rgb[1], rgb[2], alpha]


def _css(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


# ---------------------------------------------------------------------------
# Feature layers
# ---------------------------------------------------------------------------

def feature_renderer(
    geometry_type: str | None, rgb: tuple[int, int, int],
) -> dict[str, Any] | None:
    """Build the single-symbol renderer for one feature layer.

    ``geometry_type`` is the service's value (``esriGeometryPolygon``,
    ``esriGeometryPolyline``, ``esriGeometryPoint``,
    ``esriGeometryMultipoint``). Returns ``None`` for anything else
    (a table, or a type the standard does not cover).
    """
    if geometry_type == "esriGeometryPolygon":
        symbol: dict[str, Any] = {
            "type": "esriSFS",
            # Solid at zero opacity rather than "no fill" so a click
            # inside the polygon still finds it.
            "style": "esriSFSSolid",
            "color": [0, 0, 0, 0],
            "outline": {
                "type": "esriSLS",
                "style": "esriSLSSolid",
                "color": _rgba(rgb),
                "width": LINE_WIDTH_PT,
            },
        }
    elif geometry_type == "esriGeometryPolyline":
        symbol = {
            "type": "esriSLS",
            "style": "esriSLSSolid",
            "color": _rgba(rgb),
            "width": LINE_WIDTH_PT,
        }
    elif geometry_type in ("esriGeometryPoint", "esriGeometryMultipoint"):
        symbol = {
            "type": "esriSMS",
            "style": "esriSMSCircle",
            "color": _rgba(rgb),
            "size": POINT_SIZE_PT,
            "outline": {
                "type": "esriSLS",
                "style": "esriSLSSolid",
                "color": [255, 255, 255, 255],
                "width": POINT_OUTLINE_WIDTH_PT,
            },
        }
    else:
        return None
    return {"type": "simple", "symbol": symbol}


def build_feature_item_data(
    existing: Any,
    layers: list[tuple[int, str | None]],
    rgb: tuple[int, int, int],
) -> dict[str, Any]:
    """Merge the standard renderer into a feature layer item's data.

    ``layers`` is ``[(layer id, geometry type), ...]`` from the
    service. Only ``drawingInfo.renderer`` is set; pop-ups, labels and
    anything else already saved on the item are kept.
    """
    data: dict[str, Any] = (
        copy.deepcopy(existing) if isinstance(existing, dict) else {}
    )
    entries = data.get("layers")
    if not isinstance(entries, list):
        entries = []
    data["layers"] = entries
    styled = 0
    for layer_id, geometry_type in layers:
        renderer = feature_renderer(geometry_type, rgb)
        if renderer is None:
            continue
        entry = next(
            (e for e in entries
             if isinstance(e, dict) and e.get("id") == layer_id),
            None,
        )
        if entry is None:
            entry = {"id": layer_id}
            entries.append(entry)
        definition = entry.get("layerDefinition")
        if not isinstance(definition, dict):
            definition = {}
            entry["layerDefinition"] = definition
        drawing = definition.get("drawingInfo")
        if not isinstance(drawing, dict):
            drawing = {}
            definition["drawingInfo"] = drawing
        drawing["renderer"] = renderer
        styled += 1
    if not styled:
        raise SymbologyError(
            "the service has no point, line or polygon layer to style"
        )
    return data


# ---------------------------------------------------------------------------
# Rasters
# ---------------------------------------------------------------------------

def _viridis_colour_ramp() -> dict[str, Any]:
    segments = []
    for start, end in pairwise(VIRIDIS_STOPS):
        segments.append({
            "type": "algorithmic",
            "algorithm": "esriCIELabAlgorithm",
            "fromColor": _rgba(hex_to_rgb(start)),
            "toColor": _rgba(hex_to_rgb(end)),
        })
    return {"type": "multipart", "colorRamps": segments}


def continuous_renderer(statistics: tuple[float, float, float, float]) -> dict[str, Any]:
    """Stretch renderer on the viridis ramp.

    ``statistics`` is ``(low, high, mean, standard deviation)``. The
    low and high values are the stretch limits (the 2nd and 98th
    percentile of the data), fixed so colours do not shift as the map
    is panned.
    """
    low, high, mean, std = statistics
    return {
        "type": "rasterStretch",
        "stretchType": "minMax",
        "statistics": [[low, high, mean, std]],
        "min": 0,
        "max": 255,
        "dra": False,
        "useGamma": False,
        "colorRamp": _viridis_colour_ramp(),
    }


def palette_for(count: int) -> list[tuple[int, int, int]]:
    """Return ``count`` discrete colours (``count`` <= 20)."""
    if count > MAX_PALETTE_CLASSES:
        raise NoStandardStyle(
            f"{count} classes is more than the {MAX_PALETTE_CLASSES}-colour "
            f"standard palette covers"
        )
    colours = OKABE_ITO + EXTENDED_COLOURS
    return [hex_to_rgb(c) for c in colours[:count]]


def categorical_renderer(
    values: list[int],
    colormap: dict[int, tuple[int, ...]] | None,
) -> dict[str, Any]:
    """Colour-per-value renderer for a categorical raster.

    Uses the file's own colour table when it has one (styling that
    came with the data is kept); otherwise the standard palette, in
    class-value order.
    """
    if not values:
        raise SymbologyError("the raster has no data values to colour")
    ordered = sorted(values)
    if colormap:
        colours = [tuple(colormap.get(v, (0, 0, 0, 255))[:3]) for v in ordered]
    else:
        colours = palette_for(len(ordered))
    return {
        "type": "rasterColormap",
        "colormapInfos": [
            {"value": int(v), "color": _rgba(tuple(c)), "label": str(int(v))}
            for v, c in zip(ordered, colours)
        ],
    }


def read_raster_summary(path: Path, classification: str) -> dict[str, Any]:
    """Read what a raster renderer needs from the library file.

    Returns ``{"statistics": (low, high, mean, std)}`` for a continuous
    raster, or ``{"values": [...], "colormap": {...} | None}`` for a
    categorical one. NoData is excluded.
    """
    import numpy as np
    import rasterio

    with rasterio.open(path) as src:
        if classification == "categorical":
            try:
                colormap = dict(src.colormap(1)) or None
            except (ValueError, rasterio.errors.RasterioError):
                colormap = None
            return {
                "values": _unique_values(src, np),
                "colormap": colormap,
            }
        sample = _read_sample(src, np)
        valid = sample.compressed()
        if valid.size == 0:
            raise SymbologyError("the raster has no data values to stretch")
        valid = valid[np.isfinite(valid)] if valid.dtype.kind == "f" else valid
        if valid.size == 0:
            raise SymbologyError("the raster has no finite values to stretch")
        low, high = (
            float(v) for v in np.percentile(valid, list(STRETCH_PERCENTILES))
        )
        if high <= low:
            low, high = float(valid.min()), float(valid.max())
        if high <= low:
            high = low + 1.0
        return {
            "statistics": (low, high, float(valid.mean()), float(valid.std())),
        }


def _read_sample(src, np):
    """Masked read of band 1, reduced so neither side exceeds the cap."""
    scale = max(src.width, src.height) / _SAMPLE_MAX_DIM
    if scale <= 1:
        return src.read(1, masked=True)
    out_shape = (max(1, int(src.height / scale)), max(1, int(src.width / scale)))
    return src.read(1, masked=True, out_shape=out_shape)


def _unique_values(src, np) -> list[int]:
    """Distinct non-NoData values of band 1."""
    if src.width * src.height > _FULL_SCAN_MAX_PIXELS:
        # Too large to read in full; a reduced copy can miss rare classes.
        sample = _read_sample(src, np)
        return [int(v) for v in np.unique(sample.compressed())]
    found: set[int] = set()
    for _, window in src.block_windows(1):
        block = src.read(1, window=window, masked=True)
        found.update(int(v) for v in np.unique(block.compressed()))
    return sorted(found)


def raster_renderer(
    path: Path, classification: str | None,
) -> tuple[dict[str, Any], list[str]]:
    """Build the renderer for a library raster. Returns (renderer, notes)."""
    notes: list[str] = []
    if classification == "continuous":
        summary = read_raster_summary(path, "continuous")
        return continuous_renderer(summary["statistics"]), notes
    if classification == "categorical":
        summary = read_raster_summary(path, "categorical")
        values, colormap = summary["values"], summary["colormap"]
        if not colormap and len(values) > MAX_PALETTE_CLASSES:
            raise NoStandardStyle(
                f"the raster has {len(values)} classes and no colour table "
                f"of its own; the standard palette covers "
                f"{MAX_PALETTE_CLASSES}, so the AGOL default look was kept"
            )
        if not colormap and len(values) > COLOUR_BLIND_SAFE_CLASSES:
            notes.append(
                f"default style uses {len(values)} class colours; beyond "
                f"{COLOUR_BLIND_SAFE_CLASSES} they are not all "
                f"colour-blind safe, so consider restyling by hand"
            )
        return categorical_renderer(values, colormap), notes
    raise SymbologyError(
        f"classification {classification!r} is not a raster classification"
    )


def build_imagery_item_data(existing: Any, renderer: dict[str, Any]) -> dict[str, Any]:
    """Merge a raster renderer and the standard opacity into item data."""
    data: dict[str, Any] = (
        copy.deepcopy(existing) if isinstance(existing, dict) else {}
    )
    definition = data.get("layerDefinition")
    if not isinstance(definition, dict):
        definition = {}
        data["layerDefinition"] = definition
    drawing = definition.get("drawingInfo")
    if not isinstance(drawing, dict):
        drawing = {}
        definition["drawingInfo"] = drawing
    drawing["renderer"] = renderer
    data["opacity"] = RASTER_OPACITY
    return data


# ---------------------------------------------------------------------------
# Vector tile layers
# ---------------------------------------------------------------------------

def read_vtpk_style(vtpk_path: Path) -> dict[str, Any]:
    """Return the style (``root.json``) stored inside a ``.vtpk``."""
    try:
        with zipfile.ZipFile(vtpk_path) as package:
            name = next(
                (n for n in package.namelist()
                 if n.endswith(_VTPK_STYLE_SUFFIX)),
                None,
            )
            if name is None:
                raise SymbologyError(
                    f"no style file ({_VTPK_STYLE_SUFFIX}) inside "
                    f"{vtpk_path.name}"
                )
            style = json.loads(package.read(name).decode("utf-8-sig"))
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise SymbologyError(
            f"could not read the style inside {vtpk_path.name}: {exc}"
        ) from exc
    if not isinstance(style, dict) or not isinstance(style.get("layers"), list):
        raise SymbologyError(
            f"the style inside {vtpk_path.name} has no list of layers"
        )
    return style


def _is_plain(value: Any) -> bool:
    """True for a constant paint/layout value (not a data-driven one)."""
    return not isinstance(value, (dict, list))


def is_single_symbol_style(style: dict[str, Any]) -> bool:
    """True when every layer in the package is drawn with one symbol.

    ArcGIS Pro writes one drawing rule per layer for a single-symbol
    layer (two when a polygon has a wide outline). A layer the steward
    symbolised by attribute, labelled, or gave data-driven values has
    filters, text or expressions; that is deliberate styling and is
    left alone.
    """
    rules = [r for r in style.get("layers", []) if isinstance(r, dict)]
    drawn = [r for r in rules if r.get("type") != "background"]
    if not drawn:
        return False
    for rule in drawn:
        if rule.get("type") not in ("fill", "line", "circle", "symbol"):
            return False
        if not rule.get("source-layer"):
            return False
        if "filter" in rule:
            return False
        layout = rule.get("layout") or {}
        paint = rule.get("paint") or {}
        if "text-field" in layout:
            return False
        if not all(_is_plain(v) for v in paint.values()):
            return False
        if not all(_is_plain(v) for k, v in layout.items() if k != "text-font"):
            return False
    per_layer: dict[str, list[str]] = {}
    for rule in drawn:
        per_layer.setdefault(rule["source-layer"], []).append(rule["type"])
    for kinds in per_layer.values():
        if len(kinds) > 2:
            return False
        if len(kinds) == 2 and sorted(kinds) != ["fill", "line"]:
            return False
    return True


def standard_vector_tile_style(
    style: dict[str, Any],
    rgb: tuple[int, int, int],
    service_url: str | None,
) -> dict[str, Any]:
    """Rewrite a single-symbol package style to the standard.

    Polygons become a transparent fill plus an outline, lines a line,
    points a dot, all in ``rgb``. Zoom limits on the original rules
    are kept. Relative links in the style are made absolute because
    the copy is stored on the item, not next to the tiles.
    """
    if not is_single_symbol_style(style):
        raise NoStandardStyle(
            "the vector tile package has its own styling (several rules "
            "per layer, labels or filters), so it was kept as built in "
            "ArcGIS Pro"
        )
    colour = _css(rgb)
    out = copy.deepcopy(style)
    rules = [r for r in style["layers"] if isinstance(r, dict)]
    new_rules: list[dict[str, Any]] = [
        copy.deepcopy(r) for r in rules if r.get("type") == "background"
    ]
    seen: list[str] = []
    for rule in rules:
        name = rule.get("source-layer")
        if rule.get("type") == "background" or name in seen:
            continue
        seen.append(name)
        kinds = {r.get("type") for r in rules if r.get("source-layer") == name}
        base = {
            "source": rule.get("source", "esri"),
            "source-layer": name,
        }
        for key in ("minzoom", "maxzoom"):
            if key in rule:
                base[key] = rule[key]
        rule_id = rule.get("id") or name
        if "fill" in kinds:
            new_rules.append({
                "id": rule_id, "type": "fill", **base,
                "layout": {},
                "paint": {"fill-color": "rgba(0,0,0,0)"},
            })
            new_rules.append({
                "id": f"{rule_id}/outline", "type": "line", **base,
                "layout": {"line-join": "round"},
                "paint": {
                    "line-color": colour,
                    "line-width": _pt_to_px(LINE_WIDTH_PT),
                },
            })
        elif "line" in kinds:
            new_rules.append({
                "id": rule_id, "type": "line", **base,
                "layout": {"line-join": "round", "line-cap": "round"},
                "paint": {
                    "line-color": colour,
                    "line-width": _pt_to_px(LINE_WIDTH_PT),
                },
            })
        else:
            new_rules.append({
                "id": rule_id, "type": "circle", **base,
                "layout": {},
                "paint": {
                    "circle-color": colour,
                    "circle-radius": _pt_to_px(POINT_SIZE_PT) / 2,
                    "circle-stroke-color": "#FFFFFF",
                    "circle-stroke-width": _pt_to_px(POINT_OUTLINE_WIDTH_PT),
                },
            })
    out["layers"] = new_rules
    return _absolute_style_links(out, service_url)


def _pt_to_px(points: float) -> float:
    return round(points * 96 / 72, 2)


def _absolute_style_links(
    style: dict[str, Any], service_url: str | None,
) -> dict[str, Any]:
    if not service_url:
        return style
    base = service_url.rstrip("/")
    sprite = style.get("sprite")
    if isinstance(sprite, str) and sprite.startswith(".."):
        style["sprite"] = f"{base}/resources/sprites/sprite"
    glyphs = style.get("glyphs")
    if isinstance(glyphs, str) and glyphs.startswith(".."):
        style["glyphs"] = f"{base}/resources/fonts/{{fontstack}}/{{range}}.pbf"
    for source in (style.get("sources") or {}).values():
        if isinstance(source, dict) and str(source.get("url", "")).startswith(".."):
            source["url"] = base
    return style


def vtpk_layer_names(style: dict[str, Any]) -> set[str]:
    """Names of the data layers a style draws."""
    return {
        r["source-layer"] for r in style.get("layers", [])
        if isinstance(r, dict) and r.get("source-layer")
    }


# ---------------------------------------------------------------------------
# Reading and writing styles on AGOL items
# ---------------------------------------------------------------------------

_VTL_STYLE_FOLDER = "styles"
_VTL_STYLE_FILE = "root.json"


def _feature_layers(item: Any) -> list[tuple[int, str | None]]:
    from arcgis.features import FeatureLayerCollection

    collection = FeatureLayerCollection.fromitem(item)
    layers: list[tuple[int, str | None]] = []
    for layer in collection.layers or []:
        props = layer.properties
        layers.append((int(props["id"]), props.get("geometryType")))
    return layers


def _write_item_data(item: Any, data: dict[str, Any]) -> None:
    ok = item.update(item_properties={"text": json.dumps(data)})
    if ok is False:
        raise SymbologyError("AGOL did not accept the item's style data")


def _read_item_data(item: Any) -> Any:
    data = item.get_data()
    return data if isinstance(data, dict) else {}


def read_item_vector_tile_style(item: Any) -> dict[str, Any] | None:
    """Return the style saved on a vector tile layer item, if any."""
    try:
        saved = item.resources.get(
            f"{_VTL_STYLE_FOLDER}/{_VTL_STYLE_FILE}", try_json=True,
        )
    except Exception:
        return None
    return saved if isinstance(saved, dict) and saved.get("layers") else None


def _write_item_vector_tile_style(item: Any, style: dict[str, Any]) -> None:
    text = json.dumps(style)
    try:
        item.resources.add(
            folder_name=_VTL_STYLE_FOLDER, file_name=_VTL_STYLE_FILE, text=text,
        )
    except Exception:
        # Already present (the item has a saved style): replace it.
        item.resources.update(
            folder_name=_VTL_STYLE_FOLDER, file_name=_VTL_STYLE_FILE, text=text,
        )


def build_style(
    *,
    item: Any,
    row: dict[str, Any],
    target: str,
    source_path: Path,
    vtpk_path: Path | None,
) -> tuple[str, Any, list[str]]:
    """Work out the standard style for a published layer.

    Returns ``(kind, payload, notes)`` where ``kind`` is ``"item-data"``
    (feature and imagery layers) or ``"vector-tile-style"``.

    Raises:
        NoStandardStyle: the layer keeps its own styling.
        SymbologyError: the style could not be built.
    """
    rgb = category_colour(row.get("category"))
    if target == "feature-layer":
        data = build_feature_item_data(
            _read_item_data(item), _feature_layers(item), rgb,
        )
        return "item-data", data, []
    if target == "imagery-layer":
        renderer, notes = raster_renderer(source_path, row.get("classification"))
        return "item-data", build_imagery_item_data(
            _read_item_data(item), renderer,
        ), notes
    if target == "vector-tile-layer":
        if vtpk_path is None:
            raise SymbologyError("no vector tile package to read the style from")
        style = standard_vector_tile_style(
            read_vtpk_style(vtpk_path), rgb, getattr(item, "url", None),
        )
        return "vector-tile-style", style, []
    raise SymbologyError(f"no default style for agol_format {target!r}")


def write_style(item: Any, kind: str, payload: Any) -> None:
    """Write a style built by :func:`build_style` to the AGOL item."""
    if kind == "item-data":
        _write_item_data(item, payload)
    else:
        _write_item_vector_tile_style(item, payload)


_EXPECTED_ITEM_TYPES = {
    "feature-layer": "Feature Service",
    "imagery-layer": "Image Service",
    "vector-tile-layer": "Vector Tile Service",
}


def apply_default_style(
    *,
    item: Any,
    row: dict[str, Any],
    target: str,
    source_path: Path,
    vtpk_path: Path | None,
    warn: Warn,
) -> bool:
    """Give a newly published layer the standard default style.

    Called by push on the **first publish only**. Never raises: a
    failure, or a layer that keeps its own styling, is reported through
    ``warn`` and the push carries on. Returns True when a style was
    written.
    """
    expected = _EXPECTED_ITEM_TYPES.get(target)
    if expected is None or getattr(item, "type", None) != expected:
        # For example the feature-layer fallback, where the item is the
        # GeoPackage itself and has nothing to draw.
        return False
    try:
        kind, payload, notes = build_style(
            item=item, row=row, target=target,
            source_path=source_path, vtpk_path=vtpk_path,
        )
        write_style(item, kind, payload)
    except NoStandardStyle as exc:
        warn(f"default style not applied: {exc}")
        return False
    except Exception as exc:
        warn(
            f"default style could not be set, so the layer has the AGOL "
            f"default look; run `y2y agol-sync restyle {row.get('dataset_id')}` "
            f"to try again. Underlying: {exc}"
        )
        return False
    for note in notes:
        warn(note)
    return True


# ---------------------------------------------------------------------------
# restyle: put one existing layer back on the standard
# ---------------------------------------------------------------------------

def _backup_current_style(
    item: Any, target: str, reports_dir: Path, dataset_id: str, stamp: str,
) -> Path:
    """Save the style about to be replaced. Raises if it cannot be saved."""
    if target == "vector-tile-layer":
        current: Any = read_item_vector_tile_style(item)
        where = "item resource styles/root.json"
    else:
        current = item.get_data()
        where = "item data"
    backup = {
        "dataset_id": dataset_id,
        "agol_item_id": getattr(item, "id", None),
        "agol_format": target,
        "saved_at": stamp,
        "stored_in": where,
        "note": (
            "Style replaced by `y2y agol-sync restyle`. `style` is what "
            "was saved on the AGOL item before the reset; null means "
            "the item had no saved style of its own."
        ),
        "style": current if isinstance(current, (dict, list)) else None,
    }
    safe_stamp = "".join(ch for ch in stamp if ch.isalnum())
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"restyle_{dataset_id}_{safe_stamp}.json"
    path.write_text(json.dumps(backup, indent=2), encoding="utf-8")
    return path


def describe_standard(row: dict[str, Any], target: str) -> str:
    """One line saying what restyle would apply, for the confirmation."""
    if target == "imagery-layer":
        if row.get("classification") == "continuous":
            return "viridis colour ramp, 30% transparent"
        return (
            "the file's own colour table if it has one, otherwise the "
            "standard class colours; 30% transparent"
        )
    colour = CATEGORY_COLOURS.get(row.get("category") or "", FALLBACK_COLOUR)
    return (
        f"single symbol in the {row.get('category')} colour ({colour}); "
        f"polygons as an outline with no fill"
    )


def restyle(
    db_path: Path,
    dataset_id: str,
    gis: Any,
    *,
    library_root: Path,
    reports_dir: Path,
    actor: str,
) -> tuple[Path, str]:
    """Reset one published layer to the standard default style.

    The only way the pipeline overwrites a style after first publish,
    and only when the steward runs it. Saves the style it replaces to
    ``reports/restyle_<dataset_id>_<timestamp>.json`` first, and stops
    without touching the layer if that file cannot be written. Changes
    the drawing style only. The catalogue row is not changed; the run
    is recorded in the changelog.

    Returns ``(backup path, note)``.

    Raises:
        AgolError: the row is not published, the layer keeps its own
            styling, or AGOL rejected the write.
    """
    from . import agol_vtpk, inventory_manager
    from .agol_sync import AgolError, _resolve_service_item
    from .utils import utc_now_iso

    row = inventory_manager.get_dataset(db_path, dataset_id)
    if row is None:
        raise AgolError(f"dataset_id {dataset_id!r} not in catalogue")
    item_id = row.get("agol_item_id")
    if not item_id:
        raise AgolError(
            f"row {dataset_id!r} has no agol_item_id; it is not published, "
            f"so there is nothing to restyle"
        )
    target = row.get("agol_format")
    expected = _EXPECTED_ITEM_TYPES.get(target)
    if expected is None:
        raise AgolError(f"no default style for agol_format {target!r}")

    item = _resolve_service_item(gis, item_id)
    if getattr(item, "type", None) != expected:
        raise AgolError(
            f"AGOL item {item_id} is a {getattr(item, 'type', None)!r}, not "
            f"a {expected!r}; there is no layer style to reset"
        )

    source_path = library_root / row["file_path"]
    vtpk_path = None
    if target == "vector-tile-layer":
        vtpk_path = agol_vtpk.resolve_vtpk_path(row, library_root)
        if not vtpk_path.exists():
            raise AgolError(
                f"no VTPK found for {dataset_id!r} at {vtpk_path}; the "
                f"standard style is worked out from the package"
            )
    elif target == "imagery-layer" and not source_path.exists():
        raise AgolError(f"source file missing for {dataset_id!r}: {source_path}")

    try:
        kind, payload, notes = build_style(
            item=item, row=row, target=target,
            source_path=source_path, vtpk_path=vtpk_path,
        )
    except NoStandardStyle as exc:
        raise AgolError(f"not restyled: {exc}") from exc
    except Exception as exc:
        raise AgolError(
            f"could not work out the standard style for {dataset_id!r}: {exc}"
        ) from exc

    now = utc_now_iso()
    try:
        backup_path = _backup_current_style(
            item, target, reports_dir, dataset_id, now,
        )
    except Exception as exc:
        raise AgolError(
            f"could not save a backup of the current style, so the layer "
            f"was left unchanged: {exc}"
        ) from exc

    try:
        write_style(item, kind, payload)
    except Exception as exc:
        raise AgolError(
            f"AGOL rejected the standard style for {dataset_id!r}; the "
            f"layer was left as it was. Underlying: {exc}"
        ) from exc

    note_parts = [
        "reset AGOL layer style to the standard default",
        f"item_id={item_id}",
        f"previous style saved to reports/{backup_path.name}",
    ]
    note_parts.extend(notes)
    note = " | ".join(note_parts)
    inventory_manager.append_changelog(
        db_path,
        timestamp=now,
        action="metadata",
        dataset_id=dataset_id,
        actor=actor,
        path=row.get("file_path"),
        detail=note,
    )
    return backup_path, note
