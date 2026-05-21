"""Configuration for the AGOL integration.

Single source of truth for the values the AGOL integration needs:

* AGOL portal URL.
* The name of the OAuth profile cached in ``~/.arcgis/profile_<name>``.
* The OAuth ``client_id`` (a public identifier — but kept out of git
  via the ``Y2Y_AGOL_CLIENT_ID`` environment variable).
* The default folder prefix under which items are organised in the
  steward's My Content tree.
* The name of the Y2Y Conservation Atlas group; its AGOL group ID is
  resolved lazily and cached locally.
* The toggle for auto-sync on catalogue mutations
  (``Y2Y_AGOL_AUTO_PUSH``, default ``true``).

Values are sourced from (in order of precedence):

1. The instance returned by :func:`load_config`'s explicit kwargs
   (used by tests that need to inject a stub config).
2. The optional YAML file at ``~/.y2y/agol_config.yaml``.
3. Environment variables.
4. Hardcoded defaults below.

The result is an immutable ``AgolConfig`` dataclass. No global state
in this module; callers pass the loaded config around explicitly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PORTAL_URL = "https://www.arcgis.com"
DEFAULT_PROFILE_NAME = "y2y"
DEFAULT_FOLDER_PREFIX = "Y2Y_Library"
DEFAULT_CONSERVATION_ATLAS_GROUP_NAME = "Y2Y Conservation Atlas"
DEFAULT_AUTO_PUSH = True

# Env-var names recognised at load time. Kept as constants here so
# tests can monkeypatch them cleanly.
ENV_CLIENT_ID = "Y2Y_AGOL_CLIENT_ID"
ENV_AUTO_PUSH = "Y2Y_AGOL_AUTO_PUSH"
ENV_PROFILE_OVERRIDE = "Y2Y_AGOL_PROFILE"

# Path to the optional YAML override file.
CONFIG_YAML_PATH = Path.home() / ".y2y" / "agol_config.yaml"

# Path to the cached resolved values (group IDs etc.).
CACHE_DIR = Path.home() / ".y2y"
GROUP_CACHE_PATH = CACHE_DIR / "agol_group_cache.json"


@dataclass(frozen=True)
class AgolConfig:
    """Loaded, validated AGOL integration configuration."""

    portal_url: str = DEFAULT_PORTAL_URL
    profile_name: str = DEFAULT_PROFILE_NAME
    folder_prefix: str = DEFAULT_FOLDER_PREFIX
    conservation_atlas_group_name: str = DEFAULT_CONSERVATION_ATLAS_GROUP_NAME
    auto_push: bool = DEFAULT_AUTO_PUSH

    # Optional — only set after the steward has run `y2y agol-sync
    # login` at least once. The OAuth client_id is normally sourced
    # from the env var so it doesn't end up in version control.
    client_id: str | None = None

    # Lazily resolved by pipeline.agol_sync.resolve_group_id() on
    # first AGOL contact. Persisted across runs in
    # ``~/.y2y/agol_group_cache.json`` so each invocation doesn't
    # re-query the org. None means "look it up the next time we have
    # a GIS connection."
    conservation_atlas_group_id: str | None = None

    # The raw YAML payload (if a file existed), kept for debugging
    # callers that want to surface "what config was loaded from
    # disk." Not used internally; consumers should access typed
    # fields above.
    _raw_yaml: dict[str, Any] = field(default_factory=dict)


def _read_yaml_overrides(path: Path) -> dict[str, Any]:
    """Read ``~/.y2y/agol_config.yaml`` if present, else return ``{}``."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RuntimeError(
            f"Failed to parse {path} as YAML: {exc}"
        ) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RuntimeError(
            f"{path} must be a YAML mapping at top level; got {type(data).__name__}."
        )
    return data


def _read_group_cache(path: Path = GROUP_CACHE_PATH) -> dict[str, str]:
    """Read the cached AGOL group-name → group-id mapping."""
    if not path.exists():
        return {}
    import json
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _parse_bool_env(value: str | None, default: bool) -> bool:
    """Parse a permissive boolean env var ('true'/'false'/'1'/'0')."""
    if value is None:
        return default
    s = value.strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    raise RuntimeError(
        f"Cannot parse {value!r} as a boolean. "
        f"Use 'true' or 'false'."
    )


def load_config(
    *,
    yaml_path: Path | None = None,
    env: dict[str, str] | None = None,
    group_cache_path: Path | None = None,
) -> AgolConfig:
    """Load + validate the AGOL config.

    Tests inject ``yaml_path``, ``env``, and ``group_cache_path`` to
    exercise specific permutations. Production callers pass nothing
    and get the standard layered load.
    """
    yaml_path = yaml_path or CONFIG_YAML_PATH
    env = env if env is not None else os.environ
    group_cache_path = group_cache_path or GROUP_CACHE_PATH

    overrides = _read_yaml_overrides(yaml_path)
    group_cache = _read_group_cache(group_cache_path)

    profile_name = (
        env.get(ENV_PROFILE_OVERRIDE)
        or overrides.get("profile_name")
        or DEFAULT_PROFILE_NAME
    )
    portal_url = overrides.get("portal_url") or DEFAULT_PORTAL_URL
    folder_prefix = overrides.get("folder_prefix") or DEFAULT_FOLDER_PREFIX
    group_name = (
        overrides.get("conservation_atlas_group_name")
        or DEFAULT_CONSERVATION_ATLAS_GROUP_NAME
    )
    auto_push = _parse_bool_env(env.get(ENV_AUTO_PUSH), DEFAULT_AUTO_PUSH)
    client_id = env.get(ENV_CLIENT_ID) or overrides.get("client_id")

    cached_group_id = group_cache.get(group_name)

    return AgolConfig(
        portal_url=portal_url,
        profile_name=profile_name,
        folder_prefix=folder_prefix,
        conservation_atlas_group_name=group_name,
        conservation_atlas_group_id=cached_group_id,
        auto_push=auto_push,
        client_id=client_id,
        _raw_yaml=overrides,
    )


def cache_group_id(
    group_name: str,
    group_id: str,
    *,
    path: Path | None = None,
) -> None:
    """Persist a resolved group-name → group-id mapping to disk."""
    path = path or GROUP_CACHE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    existing = _read_group_cache(path)
    existing[group_name] = group_id
    path.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")
