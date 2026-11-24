"""Y2Y Spatial Library pipeline package.

Tooling for ingesting, inventorying, and reconciling spatial datasets
in the Y2Y authoritative spatial data library. The library/ directory
is the canonical data store; this package is the only sanctioned way
to mutate it.
"""

# The one place the version is set. pyproject.toml reads it from here, and
# `y2y --version` prints it. Bump it and add a CHANGELOG.md entry per release.
__version__ = "0.9.0"
