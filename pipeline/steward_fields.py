"""Parsing and normalising of steward-typed values.

The steward types these values into ``pending.xlsx`` at ingest, or passes
them to ``y2y update --set``. Each function turns what was typed into the
one form the catalogue stores, and raises ``ValueError`` with a message
written for the steward when the value cannot be understood.

Tags
    Stored as a ``;``-delimited string. The steward may separate tags with
    ``;`` or ``,`` (ArcGIS Online itself separates tags with commas, so a
    tag can never contain one). A dataset with no tags stores the marker
    ``NA``; the cell is never left empty, so "no tags" is always a
    deliberate choice. The marker is recognised in any letter case and is
    never sent to ArcGIS Online as a tag.

Authoritative
    Stored as ``1`` / ``0`` (DESIGN.md §13: INTEGER for booleans). Shown
    and typed as ``yes`` / ``no``.

Temporal coverage
    ``temporal_start`` / ``temporal_end`` are stored as ISO-8601 UTC
    strings with a ``Z`` suffix. The steward may type a year, a year and
    month, or a full date.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from typing import Any

# --- tags ---------------------------------------------------------------

TAG_SEPARATOR = ";"
NO_TAGS_MARKER = "NA"

# Spellings of the "no tags" marker, compared case-insensitively.
_NO_TAGS_SPELLINGS = frozenset({"na", "n/a"})
_TAG_SPLIT = re.compile(r"[;,]")


def is_no_tags_marker(token: str) -> bool:
    """True if ``token`` is the "no tags" marker in any letter case."""
    return token.strip().casefold() in _NO_TAGS_SPELLINGS


def parse_tags(value: Any) -> list[str]:
    """Return the real tags in ``value``, in order, without duplicates.

    ``value`` is a ``;``- or ``,``-separated string, or a list of tags.
    The "no tags" marker on its own gives an empty list. Duplicates are
    dropped case-insensitively, keeping the first spelling.

    Raises ``ValueError`` if the value is empty, or if the marker is
    mixed with real tags.
    """
    if isinstance(value, (list, tuple)):
        tokens = [str(t).strip() for t in value]
    elif value is None:
        tokens = []
    else:
        tokens = [t.strip() for t in _TAG_SPLIT.split(str(value))]
    tokens = [t for t in tokens if t]

    if not tokens:
        raise ValueError(
            f"tags is empty; list tags separated by '{TAG_SEPARATOR}', "
            f"or enter {NO_TAGS_MARKER} if the dataset should have no tags"
        )

    markers = [t for t in tokens if is_no_tags_marker(t)]
    if markers and len(tokens) > 1:
        raise ValueError(
            f"tags mixes '{markers[0]}' with real tags; enter "
            f"{NO_TAGS_MARKER} on its own for no tags, or remove it"
        )
    if markers:
        return []

    seen: set[str] = set()
    unique: list[str] = []
    for token in tokens:
        key = token.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(token)
    return unique


def normalize_tags(value: Any) -> str:
    """Return the catalogue form of ``value``: ``a;b;c`` or ``NA``."""
    tags = parse_tags(value)
    return TAG_SEPARATOR.join(tags) if tags else NO_TAGS_MARKER


def tags_for_agol(value: Any) -> list[str]:
    """Return the tag list to send to (or compare with) ArcGIS Online.

    Lenient where :func:`parse_tags` is strict: an empty value gives an
    empty list, and the "no tags" marker is dropped wherever it appears,
    so it can never be published as a tag.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        tokens = [str(t).strip() for t in value]
    else:
        tokens = [t.strip() for t in _TAG_SPLIT.split(str(value))]
    return [t for t in tokens if t and not is_no_tags_marker(t)]


# --- authoritative ------------------------------------------------------

_YES = frozenset({"yes", "y", "true", "t", "1"})
_NO = frozenset({"no", "n", "false", "f", "0"})


def parse_authoritative(value: Any) -> int:
    """Return ``1`` or ``0`` for a steward-typed yes/no value."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)) and value in (0, 1):
        return int(value)
    if isinstance(value, str):
        key = value.strip().casefold()
        if key in _YES:
            return 1
        if key in _NO:
            return 0
    raise ValueError(f"authoritative must be 'yes' or 'no' (got {value!r})")


def format_authoritative(value: Any) -> str | None:
    """Return ``yes`` / ``no`` for display, or ``None`` if unset."""
    if value is None or value == "":
        return None
    return "yes" if parse_authoritative(value) else "no"


# --- temporal coverage --------------------------------------------------

_YEAR = re.compile(r"^(\d{4})$")
_YEAR_MONTH = re.compile(r"^(\d{4})-(\d{1,2})$")
_FULL_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ].*)?$")


def parse_temporal(value: Any, *, end: bool = False) -> str | None:
    """Return ``value`` as ``YYYY-MM-DDT00:00:00Z``, or ``None`` if empty.

    Accepts a year (``2020``), a year and month (``2020-06``), a full
    date (``2020-06-15``), or a date cell from Excel. A year or a year
    and month is read as the first day of that period, or as its last
    day when ``end`` is true.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        day = value.date()
    elif isinstance(value, date):
        day = value
    else:
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        text = str(value).strip()
        if not text:
            return None
        try:
            if m := _YEAR.match(text):
                year = int(m.group(1))
                day = date(year, 12, 31) if end else date(year, 1, 1)
            elif m := _YEAR_MONTH.match(text):
                year, month = int(m.group(1)), int(m.group(2))
                last = calendar.monthrange(year, month)[1]
                day = date(year, month, last if end else 1)
            elif m := _FULL_DATE.match(text):
                day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            else:
                raise ValueError
        except ValueError:
            raise ValueError(
                f"{text!r} is not a date; use YYYY, YYYY-MM or YYYY-MM-DD"
            ) from None
    return f"{day.isoformat()}T00:00:00Z"


def check_temporal_order(start: str | None, end: str | None) -> None:
    """Raise ``ValueError`` if both are set and the end is before the start."""
    if start and end and end < start:
        raise ValueError(
            f"temporal_end ({end[:10]}) is before temporal_start ({start[:10]})"
        )
