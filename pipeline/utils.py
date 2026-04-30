"""Shared helpers: checksums, IDs, timestamps, naming conventions, metadata reads.

All functions here are pure or file-read-only. No mutations to library/,
queue/, or inventory/ — mutation lives in ingest.py and
inventory_manager.py.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import fiona
import rasterio
import ulid
from pyproj import CRS


_CHUNK_SIZE = 1024 * 1024  # 1 MiB

_SLUG_STRIP = re.compile(r"[^\w\s-]", re.UNICODE)
_SLUG_COLLAPSE = re.compile(r"[-\s_]+")
# Two-pass camel-case decomposition. Pass 1 catches "ABCxyz" → "AB_Cxyz"
# (acronym followed by Word). Pass 2 catches "abcXyz" → "abc_Xyz"
# (lowercase followed by Capital). We deliberately do NOT split on
# digit→capital boundaries so acronyms like "Y2Y" survive intact.
_CAMEL_PASS_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_PASS_2 = re.compile(r"([a-z])([A-Z])")


# --- checksums & stat ---------------------------------------------------

def sha256_file(path: Path) -> str:
    """Hex SHA-256 of the file at ``path``, streamed."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()


def stat_signature(path: Path) -> tuple[int, str]:
    """Return ``(size_bytes, mtime_iso_utc)``."""
    st = path.stat()
    mtime = (
        datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    return st.st_size, mtime


# --- timestamps ---------------------------------------------------------

def utc_now_iso() -> str:
    """Current UTC time, ISO-8601 with 'Z' suffix, second precision."""
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def utc_now_date() -> str:
    """Current UTC date, ISO-8601."""
    return datetime.now(tz=timezone.utc).date().isoformat()


def utc_now_compact() -> str:
    """Current UTC time as ``YYYYMMDDTHHMMSSZ`` — filesystem-safe (no colons)."""
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --- IDs ----------------------------------------------------------------

def new_dataset_id() -> str:
    """Opaque, stable dataset ID: ``ds_<26-char ULID>``.

    Pre-migration-001 the format was ``ds_<12 hex>``; migration 001
    re-keyed every existing row to ``ds_<ULID>`` and the schema's CHECK
    enforces the ``ds_`` prefix without constraining the suffix shape.
    All new IDs from this point forward are ULIDs (Crockford base32,
    26 characters, lexicographically sortable, time-prefixed).
    """
    return f"ds_{ulid.ULID()}"


# --- naming -------------------------------------------------------------

def slugify_title(title: str) -> str:
    """Title / camel-case → filesystem-safe lowercase underscore form.

    "Grizzly Bear Den Sites 2024" → "grizzly_bear_den_sites_2024"
    "Y2Y_RegionBoundary"          → "y2y_region_boundary"
    "XMLParser"                   → "xml_parser"

    Camel-case boundaries are split BEFORE lowercasing. Digit→capital
    boundaries are left alone so acronyms like "Y2Y" survive intact.
    """
    s = _CAMEL_PASS_1.sub(r"\1_\2", title)
    s = _CAMEL_PASS_2.sub(r"\1_\2", s)
    stripped = _SLUG_STRIP.sub("", s).strip().lower()
    return _SLUG_COLLAPSE.sub("_", stripped).strip("_")


def titleify_slug(slug: str) -> str:
    """lowercase_underscore → 'Title Case' display form."""
    return slug.replace("_", " ").title()


# --- file type classification ------------------------------------------

def is_vector(path: Path) -> bool:
    return path.suffix.lower() == ".gpkg"


def is_raster(path: Path) -> bool:
    return path.suffix.lower() in (".tif", ".tiff")


# --- CRS & bbox reads --------------------------------------------------

def read_vector_crs(path: Path) -> CRS | None:
    with fiona.open(path) as src:
        wkt = src.crs_wkt
    return CRS.from_wkt(wkt) if wkt else None


def read_raster_crs(path: Path) -> CRS | None:
    with rasterio.open(path) as ds:
        if not ds.crs:
            return None
        return CRS.from_wkt(ds.crs.to_wkt())


def read_vector_bbox(path: Path) -> tuple[float, float, float, float] | None:
    with fiona.open(path) as src:
        bounds = src.bounds
    if bounds is None or any(v is None for v in bounds):
        return None
    return tuple(bounds)


def read_raster_bbox(path: Path) -> tuple[float, float, float, float]:
    with rasterio.open(path) as ds:
        b = ds.bounds
    return (b.left, b.bottom, b.right, b.top)


def bbox_to_string(bbox: tuple[float, float, float, float]) -> str:
    """Serialize bbox as 'minx,miny,maxx,maxy' with 6 decimal places."""
    return ",".join(f"{v:.6f}" for v in bbox)


def crs_to_authority_string(crs: CRS) -> str:
    """'AUTHORITY:CODE' string, preferring ESRI (canonical is ESRI:102008)."""
    for auth in ("ESRI", "EPSG"):
        result = crs.to_authority(auth_name=auth)
        if result:
            return f"{result[0]}:{result[1]}"
    result = crs.to_authority()
    if result:
        return f"{result[0]}:{result[1]}"
    return "UNKNOWN"
