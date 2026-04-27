"""Filename-convention validator.

Canonical Y2Y filenames: lowercase letters, digits, underscores only;
words separated by single underscores; extension must be .gpkg, .tif,
or .tiff. See DESIGN.md §6 (Naming conventions) and §9 (format standards).
"""

from __future__ import annotations

import re
from pathlib import Path

_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*\.(?:gpkg|tif|tiff)$")


def validate_naming(path: Path) -> tuple[bool, str | None]:
    """Return ``(ok, reason)``. Reason is ``None`` on success."""
    if not _NAME_PATTERN.match(path.name):
        return False, (
            f"Filename '{path.name}' does not match the Y2Y convention: "
            "lowercase letters and digits only, words separated by single "
            "underscores, extension must be .gpkg or .tif."
        )
    return True, None
