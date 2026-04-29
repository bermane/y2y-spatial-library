"""Source → canonical-form transformations (DESIGN.md §11).

Two entry points:

    vector_to_canonical(source, target)
    raster_to_canonical(source, target, classification)

Both write the transformed file to ``target``. They never mutate the
source. Errors are raised as :class:`TransformError` with a steward-readable
message — the caller (``ingest.approve``) catches and surfaces them via
the ``_validation_error`` column.

**Passthrough optimisation.** If the source already passes the canonical
format + CRS validators (i.e. it's a GeoPackage in ESRI:102008 with valid
single-type 2D geometries, or a 512×512 ZSTD COG in ESRI:102008 with
correct predictor/dtype/NoData/overviews), the transformation is just a
``shutil.copy2`` — no re-encode, no warp, no I/O cost beyond the copy.
The destination bytes are identical to the source. This applies to most
Y2Y-internal datasets that are already produced in canonical form.

The source file must already have a valid CRS by the time it reaches
this module — ``source_formats.inspect`` rejects CRS-less files at scan
so the transformation path can rely on the file's declared CRS.

What the transformer does:

Vector (any allowed source → GeoPackage in ESRI:102008):
  - Drop / reject if no features.
  - Validate every geometry (no auto-repair).
  - Reject 3D / Z-M geometries.
  - Promote pure Polygon → MultiPolygon (and the analogous pairs for
    LineString, Point) when both types coexist; reject any other
    incompatible mix.
  - Reproject to ESRI:102008 (no-op if already there).

Raster (any allowed TIFF → COG in ESRI:102008):
  - Reject if not single-band (the source-format check already enforces
    this at scan; we re-check defensively).
  - Reject dtypes outside {float32, uint8, uint16}.
  - Reproject to ESRI:102008 with bilinear (continuous) or nearest
    (categorical) resampling.
  - Write COG with ZSTD-9, predictor 3 (continuous) / 2 (categorical),
    512×512 internal blocks, internal overviews, NoData set to the
    source's NoData if defined, else the canonical default by dtype
    (-9999 / 255 / 65535).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import fiona
import geopandas as gpd
import numpy as np
import rasterio
from pyproj import CRS
from rasterio.enums import Resampling
from rasterio.warp import calculate_default_transform, reproject
from shapely.geometry import (
    LineString, MultiLineString,
    MultiPoint, MultiPolygon,
    Point, Polygon,
)
from shapely.validation import explain_validity


CANONICAL_CRS = CRS.from_user_input("ESRI:102008")
_BLOCK_SIZE = 512
_ZSTD_LEVEL = 9
_DEFAULT_NODATA = {"float32": -9999.0, "uint8": 255, "uint16": 65535}
_PREDICTOR = {"continuous": 3, "categorical": 2}
_RESAMPLING = {"continuous": Resampling.bilinear, "categorical": Resampling.nearest}


class TransformError(Exception):
    """Raised when source data cannot be transformed into canonical form."""


# --- passthrough --------------------------------------------------------

def is_passthrough_eligible(source: Path) -> bool:
    """True if ``source`` already meets canonical format + CRS rules.

    A passthrough-eligible source can be promoted by ``shutil.copy2``
    (preserving timestamps) instead of a full re-encode. Naming is
    *not* checked here — the canonical filename is determined by
    ``target_filename`` in the review sheet, not the source name.
    """
    # Local imports to avoid a load-time cycle: validators depend on
    # utils, transformations depend on validators only at call time.
    from .validators.crs import validate_crs
    from .validators.format import validate_format

    ok, _ = validate_format(source)
    if not ok:
        return False
    ok, _ = validate_crs(source)
    if not ok:
        return False
    return True


def _passthrough(source: Path, target: Path) -> None:
    """Copy ``source`` to ``target`` byte-for-byte (with mtime/atime preserved)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    shutil.copy2(source, target)


# --- vector ------------------------------------------------------------

def vector_to_canonical(source: Path, target: Path) -> None:
    """Read ``source`` (any allowed vector format), write canonical GPKG to ``target``."""
    if is_passthrough_eligible(source):
        _passthrough(source, target)
        return

    try:
        gdf = gpd.read_file(source)
    except Exception as exc:
        raise TransformError(f"failed to read vector source: {exc}") from exc

    if gdf.empty:
        raise TransformError("source has zero features.")

    if gdf.crs is None:
        # Defensive: scan should have rejected this; fail loud if we got here.
        raise TransformError(
            "source has no CRS — should have been rejected at scan."
        )

    # Z/M dimension check — done by introspecting any geometry's `has_z`
    # via shapely (geopandas exposes it as `.has_z` on the GeoSeries).
    try:
        if bool(gdf.geometry.has_z.any()):
            raise TransformError(
                "source contains 3D (Z) geometries; canonical standard is 2D only. "
                "Strip Z dimensions in source before re-ingesting."
            )
    except AttributeError:
        # Some geometry types may not expose has_z; fall through.
        pass

    # Geometry validity
    invalid_idx = gdf.index[~gdf.geometry.is_valid].tolist()
    if invalid_idx:
        bad = gdf.loc[invalid_idx[0]].geometry
        raise TransformError(
            f"source has {len(invalid_idx)} invalid geometries (first at index "
            f"{invalid_idx[0]}: {explain_validity(bad)}). "
            f"Repair the source before re-ingesting (no auto-fix)."
        )

    # Mixed-type promotion (Polygon+MultiPolygon, etc.).
    geom_types = set(gdf.geometry.geom_type.unique())
    promotion = _resolve_geometry_promotion(geom_types)
    if promotion:
        promote_to = promotion
        gdf["geometry"] = gdf.geometry.apply(lambda g: _wrap_as_multi(g, promote_to))

    # Reproject to canonical CRS
    if not _crs_equals(gdf.crs, CANONICAL_CRS):
        gdf = gdf.to_crs(CANONICAL_CRS)

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    gdf.to_file(target, driver="GPKG")


def _resolve_geometry_promotion(geom_types: set[str]) -> str | None:
    """Return the target Multi-type if a mix should be auto-promoted, else None.

    Returns None if no promotion is needed (single concrete type).
    Raises TransformError if the mix is incompatible.
    """
    if len(geom_types) <= 1:
        return None
    compatible_pairs = {
        frozenset({"Polygon", "MultiPolygon"}): "MultiPolygon",
        frozenset({"LineString", "MultiLineString"}): "MultiLineString",
        frozenset({"Point", "MultiPoint"}): "MultiPoint",
    }
    target = compatible_pairs.get(frozenset(geom_types))
    if target is None:
        raise TransformError(
            f"source has incompatible mixed geometry types {sorted(geom_types)}; "
            f"only Polygon/MultiPolygon, LineString/MultiLineString, and "
            f"Point/MultiPoint pairs auto-promote."
        )
    return target


def _wrap_as_multi(geom, target_type: str):
    if geom is None:
        return geom
    if geom.geom_type == target_type:
        return geom
    if target_type == "MultiPolygon" and isinstance(geom, Polygon):
        return MultiPolygon([geom])
    if target_type == "MultiLineString" and isinstance(geom, LineString):
        return MultiLineString([geom])
    if target_type == "MultiPoint" and isinstance(geom, Point):
        return MultiPoint([geom])
    return geom


def _crs_equals(a, b) -> bool:
    if a is None or b is None:
        return False
    try:
        return CRS.from_user_input(a).equals(b if isinstance(b, CRS) else CRS.from_user_input(b))
    except Exception:
        return False


# --- raster ------------------------------------------------------------

def raster_to_canonical(
    source: Path,
    target: Path,
    classification: str,
) -> None:
    """Read ``source`` (single-band TIFF), write canonical COG to ``target``."""
    if classification not in _PREDICTOR:
        raise TransformError(
            f"classification must be 'continuous' or 'categorical', got '{classification}'."
        )

    if is_passthrough_eligible(source):
        # Source already meets the canonical raster profile. The only
        # remaining check: classification must agree with the source's
        # predictor (which is canonical for the source's dtype). If
        # they disagree, the steward declared a wrong classification —
        # surface it now rather than letting the post-transform validator
        # produce a less-clear "predictor mismatch" error.
        with rasterio.open(source) as src:
            struct = src.tags(ns="IMAGE_STRUCTURE") or {}
            source_predictor = struct.get("PREDICTOR")
        expected_predictor = str(_PREDICTOR[classification])
        if source_predictor != expected_predictor:
            raise TransformError(
                f"source predictor '{source_predictor}' is inconsistent with "
                f"declared classification '{classification}' "
                f"(predictor '{expected_predictor}'). The source dtype and "
                f"the declared classification don't match — change "
                f"classification or convert dtype before re-ingesting."
            )
        _passthrough(source, target)
        return

    with rasterio.open(source) as src:
        if src.count != 1:
            raise TransformError(
                f"source has {src.count} bands; Phase A supports single-band only."
            )

        dtype = src.dtypes[0].lower()
        if dtype not in _DEFAULT_NODATA:
            raise TransformError(
                f"source dtype '{dtype}' is not in canonical set "
                f"{{float32, uint8, uint16}}; convert the source dtype before re-ingesting."
            )

        src_crs = src.crs
        if not src_crs:
            # Defensive: scan should have rejected this.
            raise TransformError(
                "source has no CRS — should have been rejected at scan."
            )

        nodata = src.nodata if src.nodata is not None else _DEFAULT_NODATA[dtype]

        dst_transform, dst_width, dst_height = calculate_default_transform(
            src_crs, CANONICAL_CRS, src.width, src.height, *src.bounds,
        )

        profile = {
            "driver": "GTiff",
            "dtype": dtype,
            "count": 1,
            "crs": CANONICAL_CRS.to_wkt(),
            "transform": dst_transform,
            "width": dst_width,
            "height": dst_height,
            "tiled": True,
            "blockxsize": _BLOCK_SIZE,
            "blockysize": _BLOCK_SIZE,
            "compress": "zstd",
            "predictor": _PREDICTOR[classification],
            "zstd_level": _ZSTD_LEVEL,
            "nodata": nodata,
            "BIGTIFF": "IF_NEEDED",
        }

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()

        # Stream the reprojection through rasterio's block buffer instead
        # of allocating the whole destination as a numpy array — keeps
        # peak memory bounded for multi-billion-pixel rasters.
        with rasterio.open(target, "w", **profile) as dst:
            reproject(
                source=rasterio.band(src, 1),
                destination=rasterio.band(dst, 1),
                src_transform=src.transform,
                src_crs=src_crs,
                dst_transform=dst_transform,
                dst_crs=CANONICAL_CRS,
                resampling=_RESAMPLING[classification],
                src_nodata=src.nodata,
                dst_nodata=nodata,
            )
            dst.build_overviews([2], _RESAMPLING[classification])
