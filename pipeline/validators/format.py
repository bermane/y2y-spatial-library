"""Format validator: GeoPackage for vector, Cloud Optimized GeoTIFF for raster.

Implements the structural rules in DESIGN.md §9.

Vector (.gpkg):
  - Opens cleanly with fiona.
  - Schema declares a concrete single geometry type (no ``Unknown``,
    no mixed Polygon/MultiPolygon — must be MultiPolygon if both occur).
  - 2D only (no Z/M dimensions) unless the dataset is inherently 3D —
    we currently reject all 3D layers; relax later if a real use case appears.
  - Every feature has valid (non-self-intersecting, etc.) geometry.
  - Primary key: GPKG layers carry an integer ``fid`` by spec, which
    satisfies "stable, non-null identifier per feature". No explicit
    code check needed; relying on the format guarantee.

Raster (.tif / .tiff — Cloud Optimized GeoTIFF):
  - Opens cleanly with rasterio.
  - Driver is GTiff.
  - Internally tiled at 512×512.
  - Has internal overviews.
  - Compression is ZSTD.
  - Predictor matches data type: 3 for Float32 (continuous),
    2 for UInt8 / UInt16 (categorical).
  - Data type is one of {float32, uint8, uint16}.
  - NoData value is set (the actual sentinel may be steward-overridden;
    we just require it be defined).
"""

from __future__ import annotations

from pathlib import Path

import fiona
import rasterio
from shapely.geometry import shape
from shapely.validation import explain_validity

_CANONICAL_BLOCK_SIZE = 512

_ALLOWED_GEOMETRY_TYPES = frozenset({
    "Point", "MultiPoint",
    "LineString", "MultiLineString",
    "Polygon", "MultiPolygon",
})

_ALLOWED_RASTER_DTYPES = frozenset({"float32", "uint8", "uint16"})

# Predictor expected for each canonical data type.
_PREDICTOR_BY_DTYPE: dict[str, str] = {
    "float32": "3",
    "uint8": "2",
    "uint16": "2",
}


def validate_format(path: Path) -> tuple[bool, str | None]:
    """Return ``(ok, reason)``. Reason is ``None`` on success."""
    ext = path.suffix.lower()
    if ext == ".gpkg":
        return _validate_gpkg(path)
    if ext in (".tif", ".tiff"):
        return _validate_cog(path)
    return False, (
        f"File extension '{ext}' is not a canonical Y2Y format. "
        "Vector must be .gpkg (GeoPackage); raster must be .tif (Cloud Optimized GeoTIFF)."
    )


# --- vector -------------------------------------------------------------

def _validate_gpkg(path: Path) -> tuple[bool, str | None]:
    try:
        with fiona.open(path) as src:
            geom_type = src.schema.get("geometry") if src.schema else None

            if not geom_type or geom_type == "Unknown":
                return False, (
                    "GPKG layer has no concrete geometry type "
                    "(mixed types are not allowed; use MultiPolygon if you have both Polygon and MultiPolygon)."
                )

            # 3D / Z / M rejected (DESIGN.md §9: 2D only). Be precise about
            # the suffix check — "M" in "MultiPolygon" and "Z" anywhere in
            # a name would false-positive a substring match.
            if (
                geom_type.startswith("3D ")
                or geom_type.endswith("Z")
                or geom_type.endswith("M")
            ):
                return False, (
                    f"GPKG layer geometry '{geom_type}' has Z/M dimensions; "
                    "Y2Y standard is 2D only."
                )

            if geom_type not in _ALLOWED_GEOMETRY_TYPES:
                return False, (
                    f"GPKG layer geometry '{geom_type}' is not in the allowed set "
                    f"({sorted(_ALLOWED_GEOMETRY_TYPES)})."
                )

            # Walk every feature once: catch invalid geometries AND Z values
            # that the layer schema may not advertise (some sources carry
            # Z on features while the schema reports plain 2D).
            for idx, feat in enumerate(src):
                geom_dict = feat.get("geometry") if isinstance(feat, dict) else feat["geometry"]
                if geom_dict is None:
                    return False, f"feature index {idx} has null geometry."
                geom = shape(geom_dict)
                if geom.has_z:
                    return False, (
                        f"feature index {idx} has Z dimension; "
                        f"Y2Y standard is 2D only. Strip with `ogr2ogr -dim 2`."
                    )
                if not geom.is_valid:
                    return False, (
                        f"feature index {idx} has invalid geometry: {explain_validity(geom)}"
                    )
    except Exception as exc:
        return False, f"GeoPackage failed to open or read: {exc}"
    return True, None


# --- raster -------------------------------------------------------------

def _validate_cog(path: Path) -> tuple[bool, str | None]:
    try:
        with rasterio.open(path) as ds:
            profile = ds.profile

            if profile.get("driver") != "GTiff":
                return False, f"raster driver is '{profile.get('driver')}'; required: GTiff."

            if not profile.get("tiled", False):
                return False, "raster is not internally tiled; COG requires internal tiling."

            bx = profile.get("blockxsize")
            by = profile.get("blockysize")
            if bx != _CANONICAL_BLOCK_SIZE or by != _CANONICAL_BLOCK_SIZE:
                return False, (
                    f"raster block size is {bx}×{by}; canonical standard is "
                    f"{_CANONICAL_BLOCK_SIZE}×{_CANONICAL_BLOCK_SIZE}."
                )

            if not ds.overviews(1):
                return False, "raster has no internal overviews; COG requires overviews."

            compress = (profile.get("compress") or "").lower()
            if compress != "zstd":
                return False, (
                    f"raster compression is '{compress or 'none'}'; canonical standard is ZSTD."
                )

            dtype = (profile.get("dtype") or "").lower()
            if dtype not in _ALLOWED_RASTER_DTYPES:
                return False, (
                    f"raster dtype is '{dtype}'; canonical standard is one of "
                    f"{sorted(_ALLOWED_RASTER_DTYPES)} (Float32 continuous; UInt8/UInt16 categorical)."
                )

            image_struct = ds.tags(ns="IMAGE_STRUCTURE") or {}
            predictor = image_struct.get("PREDICTOR")
            expected_predictor = _PREDICTOR_BY_DTYPE.get(dtype)
            if expected_predictor and predictor != expected_predictor:
                return False, (
                    f"raster predictor is '{predictor}'; expected '{expected_predictor}' "
                    f"for dtype '{dtype}' (DESIGN.md §9)."
                )

            if ds.nodata is None:
                return False, "raster has no NoData value defined."
    except Exception as exc:
        return False, f"raster failed to open or read: {exc}"

    return True, None
