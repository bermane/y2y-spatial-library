"""CRS validator: ESRI:102008 (North America Albers Equal Area Conic, NAD83).

See DESIGN.md §9 (Canonical format standards).
"""

from __future__ import annotations

from pathlib import Path

from pyproj import CRS

from .. import utils

_CANONICAL_CRS = CRS.from_user_input("ESRI:102008")


def validate_crs(path: Path) -> tuple[bool, str | None]:
    """Return ``(ok, reason)``. Reason is ``None`` on success."""
    if utils.is_vector(path):
        crs = utils.read_vector_crs(path)
    elif utils.is_raster(path):
        crs = utils.read_raster_crs(path)
    else:
        return False, "Cannot check CRS: file is neither a recognized vector nor raster format."

    if crs is None:
        return False, "File has no CRS defined."

    if not crs.equals(_CANONICAL_CRS):
        observed = utils.crs_to_authority_string(crs)
        return False, f"CRS is {observed}; canonical is ESRI:102008."

    return True, None
