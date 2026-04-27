"""Validator modules for incoming spatial datasets.

Each validator returns ``(ok: bool, reason: str | None)``. ``validate_all``
runs them in the order naming → format → CRS and collects failures. If
the format check fails, CRS validation is skipped (the file can't be
opened to read its CRS).
"""

from __future__ import annotations

from pathlib import Path

from .crs import validate_crs
from .format import validate_format
from .naming import validate_naming


def validate_all(path: Path) -> list[tuple[str, str]]:
    """Run all validators against ``path`` and return any failures.

    Returns a list of ``(check_name, reason)`` tuples — empty means pass.
    """
    failures: list[tuple[str, str]] = []

    ok, reason = validate_naming(path)
    if not ok:
        failures.append(("naming", reason or "unknown"))

    ok, reason = validate_format(path)
    if not ok:
        failures.append(("format", reason or "unknown"))
        return failures  # CRS check needs a readable file

    ok, reason = validate_crs(path)
    if not ok:
        failures.append(("crs", reason or "unknown"))

    return failures


__all__ = ["validate_all", "validate_naming", "validate_format", "validate_crs"]
