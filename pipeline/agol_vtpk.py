"""Local Vector Tile Package (VTPK) build via arcpy.

When a dataset's ``agol_target='vector-tile-layer'``, the AGOL push
path needs a ``.vtpk`` file to upload — and the steward asked that
this file be built **locally** rather than by triggering AGOL's
server-side tile generation (which would create an intermediate
hosted feature layer, consuming AGOL credits, contrary to the whole
point of the VTL path).

This module's single public function, :func:`build_vtpk`, drives
``arcpy.management.CreateVectorTilePackage`` to produce a local
``.vtpk`` from a GeoPackage. Cached at ``.y2y/vtpk_cache/<dataset_id>.vtpk``,
keyed by ``checksum_sha256`` so the (expensive) tile-pyramid build
only runs when the source actually changes.

Runtime environment requirement
-------------------------------
``arcpy`` ships with ArcGIS Pro; it is **not** a pip-installable
package. The Mac-side Python that runs the rest of the Y2Y pipeline
cannot import arcpy. To publish Vector Tile Layers, the steward
must run ``y2y agol-sync push <id>`` (with ``agol_target=
'vector-tile-layer'``) under the **ArcGIS Pro bundled Python**
(typically ``C:\\Program Files\\ArcGIS\\Pro\\bin\\Python\\envs\\arcgispro-py3\\python.exe``
on Windows).

This module detects arcpy at runtime. If absent, it raises
:class:`AgolToolingError` with a clear message — the catalogue row
stays ``sync_status='pending_push'`` (or ``error``) and the steward
re-runs from the right environment later. The other publish paths
(``feature-layer``, ``imagery-layer``) are unaffected by arcpy's
absence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .agol_sync import AgolToolingError


# Scale-range defaults — intentionally None.
#
# arcpy.management.CreateVectorTilePackage rejects any min/max scale
# value that doesn't match the active tiling scheme's level scales
# *exactly* (ERROR 001856). With service_type="ONLINE" the scheme is
# fixed to ArcGIS Online's pyramid; the only safe match is the
# precise float values it expects, and "precise" here means 18-digit
# decimals like 591657527.59155178 — not 591657527.591555. Even tiny
# rounding makes the comparison fail.
#
# When these are None, we don't pass min_cached_scale /
# max_cached_scale to arcpy at all, and the tool uses the scheme's
# full default range. Tile generation is still bounded by the data
# extent (via index_polygons=None, which means "use the data
# extent"), so a "full pyramid" build doesn't generate tiles where
# there's no data. Result: correct VTPK without scale-precision
# pitfalls.
#
# A future caller who needs to cap levels can pass exact float values
# via the kwargs.
_DEFAULT_MIN_CACHED_SCALE: float | None = None
_DEFAULT_MAX_CACHED_SCALE: float | None = None

_CACHE_DIR_NAME = "vtpk_cache"
_CHECKSUM_SIDECAR_SUFFIX = ".sha256"


def build_vtpk(
    gpkg_path: Path,
    dataset_id: str,
    checksum: str,
    cache_dir: Path,
    *,
    min_cached_scale: float | None = _DEFAULT_MIN_CACHED_SCALE,
    max_cached_scale: float | None = _DEFAULT_MAX_CACHED_SCALE,
    tile_format: str = "INDEXED",
) -> Path:
    """Build (or fetch from cache) a Vector Tile Package for one dataset.

    Args:
        gpkg_path: Path to the canonical GeoPackage source.
        dataset_id: Catalogue dataset_id; used as the VTPK filename
            stem so a row's package is recoverable later.
        checksum: ``checksum_sha256`` from the catalogue row. The
            cache-validity guard compares against a sidecar file.
        cache_dir: Root cache directory (typically ``.y2y/`` in the
            project root). The VTPK lands at
            ``cache_dir/vtpk_cache/<dataset_id>.vtpk``.
        min_cached_scale, max_cached_scale: passed straight to
            arcpy's CreateVectorTilePackage. Lower max → smaller
            VTPK but coarser at high zoom. Override the defaults
            for datasets where the tile pyramid is overkill.
        tile_format: ``INDEXED`` (default) or ``FLAT``. Indexed is
            the modern default and what AGOL prefers for hosted
            Vector Tile Services.

    Returns:
        Path to the .vtpk file.

    Raises:
        AgolToolingError: arcpy isn't importable from the current
            Python environment. The catalogue should preserve
            ``sync_status='pending_push'`` so the steward can
            retry under the ArcGIS Pro Python.
        RuntimeError: arcpy is present but the package build
            failed (e.g., GPKG layer can't be opened, output
            directory not writable, license check failed).
    """
    cache_dir = Path(cache_dir)
    vtpk_dir = cache_dir / _CACHE_DIR_NAME
    vtpk_dir.mkdir(parents=True, exist_ok=True)
    out_path = vtpk_dir / f"{dataset_id}.vtpk"
    sidecar = out_path.with_suffix(out_path.suffix + _CHECKSUM_SIDECAR_SUFFIX)

    # Cache hit: same checksum, .vtpk on disk → return without rebuilding.
    if out_path.exists() and sidecar.exists():
        if sidecar.read_text(encoding="utf-8").strip() == checksum:
            return out_path

    # Cache miss — need to actually build.
    arcpy = _require_arcpy()

    if not gpkg_path.exists():
        raise RuntimeError(
            f"source GeoPackage not found: {gpkg_path}"
        )

    # arcpy.management.CreateVectorTilePackage REQUIRES a Pro Map
    # object as in_map — there's no feature-class-direct shortcut
    # in ArcGIS Pro 3.x ("ERROR 000735: Input Map: Value is
    # required" surfaces if you try to pass a feature class path).
    # So we build a throwaway ArcGIS Pro project in a temp dir,
    # add the GPKG layer to its default Map, then run the tool.
    #
    # The .aprx + map are scoped to a TemporaryDirectory so the
    # filesystem stays clean even on tool-side failures. Pro's
    # bundled blank template is the seed; arcpy.mp.ArcGISProject
    # opens it, .saveACopy() forks it to our temp location, and
    # we mutate the copy freely.
    layer_name = _resolve_gpkg_layer_name(arcpy, gpkg_path)

    # arcpy.ListFeatureClasses() on a GeoPackage returns names already
    # prefixed with "main." (the GPKG default schema). Joining with
    # an additional "main." prefix produces a bogus path like
    # "<gpkg>/main.main.<layer>" which arcpy can't open ("Failed to
    # add data. Possible credentials issue." — misleading, it's
    # really "dataset not found"). Strip the prefix if present.
    bare_layer_name = (
        layer_name[len("main."):] if layer_name.startswith("main.")
        else layer_name
    )
    layer_ref = f"{gpkg_path}\\main.{bare_layer_name}"

    # Delete any stale .vtpk at the target path — arcpy refuses to
    # overwrite by default.
    if out_path.exists():
        out_path.unlink()

    import tempfile
    # ignore_cleanup_errors=True: arcpy / Pro holds COM-level handles
    # on the .aprx that aren't released by `del aprx` alone (release
    # only happens at process exit). Without the flag, TemporaryDirectory
    # raises a secondary PermissionError on cleanup that masks the
    # real underlying error (or, on a successful build, makes the
    # caller think the build failed). Orphaned files in %TEMP% are
    # harmless — Windows cleans the dir periodically.
    with tempfile.TemporaryDirectory(
        prefix="y2y_vtpk_", ignore_cleanup_errors=True,
    ) as tmpdir:
        staging_aprx = Path(tmpdir) / "vtpk_staging.aprx"
        try:
            aprx = _open_blank_pro_project(
                arcpy, staging_aprx, cache_dir=cache_dir,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to create staging ArcGIS Pro project for VTPK "
                f"build: {exc}. Confirm ArcGIS Pro is installed and the "
                f"current Python is the Pro bundled environment."
            ) from exc

        # Wrap everything from project-open through tool invocation in
        # a try/finally so the aprx handle is ALWAYS released — even
        # when an intermediate step (addDataFromPath, etc.) raises.
        # Otherwise Windows holds the .aprx file open and
        # TemporaryDirectory.__exit__ fails its own cleanup with a
        # PermissionError, masking the real underlying error.
        try:
            maps = aprx.listMaps()
            if not maps:
                # createMap signature varies across SDK versions: try
                # the modern 'map_type' kwarg first then fall back.
                try:
                    map_obj = aprx.createMap("Y2Y_VTPK_Staging", "Map")
                except TypeError:
                    map_obj = aprx.createMap("Y2Y_VTPK_Staging")
            else:
                map_obj = maps[0]

            # Add the GPKG layer to the map.
            try:
                map_obj.addDataFromPath(layer_ref)
            except Exception as exc:
                raise RuntimeError(
                    f"arcpy could not add GPKG layer to staging map "
                    f"({layer_ref!r}): {exc}"
                ) from exc

            # Persist so CreateVectorTilePackage can see the map
            # contents.
            aprx.save()

            # Build the arcpy kwargs conditionally so we only pass
            # scale arguments when the caller explicitly provided
            # them. arcpy validates min/max scales against the active
            # tiling scheme; passing None or rounded values triggers
            # ERROR 001856.
            create_kwargs: dict[str, Any] = {
                "in_map": map_obj,
                "output_file": str(out_path),
                "service_type": "ONLINE",
                "tile_structure": "INDEXED",
                "index_polygons": None,
                "summary": f"Vector Tile Package for {dataset_id}",
                "tags": "Y2Y",
            }
            if min_cached_scale is not None:
                create_kwargs["min_cached_scale"] = min_cached_scale
            if max_cached_scale is not None:
                create_kwargs["max_cached_scale"] = max_cached_scale

            try:
                arcpy.management.CreateVectorTilePackage(**create_kwargs)
            except Exception as exc:  # pragma: no cover — arcpy-only path
                raise RuntimeError(
                    f"arcpy.management.CreateVectorTilePackage failed for "
                    f"{gpkg_path}: {exc}"
                ) from exc
        finally:
            # Release file handles so the TemporaryDirectory can clean
            # itself up without 'file in use' errors on Windows. Must
            # run regardless of where in the block above we errored.
            try:
                del aprx
            except NameError:
                pass

    if not out_path.exists():
        raise RuntimeError(
            f"arcpy reported success but no .vtpk was produced at {out_path}"
        )

    sidecar.write_text(checksum, encoding="utf-8")
    return out_path


def _open_blank_pro_project(
    arcpy: Any, dest_aprx: Path, *, cache_dir: Path | None = None,
) -> Any:
    """Open an ArcGIS Pro project at ``dest_aprx``, seeded from a blank
    template.

    arcpy.mp.ArcGISProject only opens existing .aprx files — there's
    no in-memory or 'create new' constructor. So we locate a blank
    template, saveACopy() it to ``dest_aprx``, then reopen the copy
    (which is what the caller mutates).

    Template lookup order:

    1. **Steward override** at ``<cache_dir>/vtpk_template.aprx``.
       If you have a custom template (e.g., with project-level
       defaults like a spatial reference), drop it here and the
       pipeline uses it instead of Pro's bundled one. Stable across
       Pro upgrades, so this is the recommended setup if Pro's
       bundled template location moves in a future release.
    2. **Pro's bundled blank.** Pro 3.x ships ``Blank.aprx`` under
       ``Resources\\ArcToolBox\\Services\\routingservices\\data\\``
       (unusual location, but works). We also probe a few other
       historical paths in case Esri moves it again.

    If none exist, raise with a clear remediation hint pointing at
    the cache_dir override.
    """
    candidates: list[Path] = []
    if cache_dir is not None:
        candidates.append(Path(cache_dir) / "vtpk_template.aprx")

    install_dir = Path(arcpy.GetInstallInfo()["InstallDir"])
    candidates.extend([
        # Pro 3.x — the routing services toolbox happens to ship a
        # blank .aprx; not officially documented as a template but
        # arcpy treats it like one.
        install_dir / "Resources" / "ArcToolBox" / "Services" / "routingservices" / "data" / "Blank.aprx",
        # Historical / version-specific candidate locations.
        install_dir / "Resources" / "ProjectTemplates" / "BlankTemplate.aprx",
        install_dir / "Resources" / "ArcCatalog" / "Templates" / "BlankTemplate.aprx",
        install_dir / "Resources" / "ApplicationTemplates" / "BlankTemplate.aprx",
    ])

    template: Path | None = next((c for c in candidates if c.exists()), None)
    if template is None:
        override_hint = (
            Path(cache_dir) / "vtpk_template.aprx"
            if cache_dir is not None
            else Path(".y2y") / "vtpk_template.aprx"
        )
        raise RuntimeError(
            f"Could not locate a blank ArcGIS Pro project template "
            f"under {install_dir!r}. Checked: "
            f"{[str(c) for c in candidates]}. "
            f"Fix: open ArcGIS Pro → File → New Project → "
            f"choose any 'Map' template, then save the project as "
            f"{str(override_hint)!r}. After that the pipeline picks "
            f"it up automatically on every VTPK build."
        )

    src = arcpy.mp.ArcGISProject(str(template))
    src.saveACopy(str(dest_aprx))
    # Release the source handle.
    del src
    return arcpy.mp.ArcGISProject(str(dest_aprx))


# ----------------------------------------------------------------------------
# arcpy detection
# ----------------------------------------------------------------------------

def _require_arcpy() -> Any:
    """Return the arcpy module, or raise ``AgolToolingError`` if absent.

    Kept as a function (not module-level) so importing
    ``pipeline.agol_vtpk`` works on every environment, including the
    Mac-side Python that can't import arcpy. The Mac runs everything
    *except* the VTL publish path; we only need arcpy when a
    vector-tile-layer row is actually being pushed.
    """
    try:
        import arcpy  # type: ignore[import-not-found]
    except ImportError as exc:
        raise AgolToolingError(
            "Vector Tile Layer publishing requires the ArcGIS Pro "
            "Python environment. Run this command under Pro's "
            "bundled Python (typically "
            "`C:\\Program Files\\ArcGIS\\Pro\\bin\\Python\\envs\\arcgispro-py3\\python.exe` "
            "on Windows), or change this dataset's `agol_target` to "
            "`feature-layer` to use the SDK-only publish path."
        ) from exc
    return arcpy


def is_arcpy_available() -> bool:
    """Quick boolean probe for tests / status messages.

    Doesn't raise — useful in CLI / status reporting where we want to
    show 'arcpy: available' or 'arcpy: not available (VTL pushes
    will fail)' without forcing the steward to attempt a push.
    """
    try:
        import arcpy  # noqa: F401  — import-side-effect only
        return True
    except ImportError:
        return False


# ----------------------------------------------------------------------------
# GPKG layer-name resolution
# ----------------------------------------------------------------------------

def _resolve_gpkg_layer_name(arcpy: Any, gpkg_path: Path) -> str:
    """Find the single feature layer inside a Y2Y canonical GeoPackage.

    Y2Y's ingestion pipeline writes single-layer GPKGs (multi-layer
    sources are rejected at scan). The layer name typically matches
    the file stem, but we don't assume that — we ask arcpy to list
    feature classes inside the GPKG and pick the single one.

    Raises ``RuntimeError`` if the GPKG has zero or multiple feature
    classes (shouldn't happen for catalogue rows, but defensive).
    """
    workspace = str(gpkg_path)
    # arcpy.da.Walk / ListFeatureClasses requires setting the
    # workspace. Save + restore in case the caller's arcpy session
    # had a different workspace.
    prior = arcpy.env.workspace
    try:
        arcpy.env.workspace = workspace
        fcs = list(arcpy.ListFeatureClasses() or [])
    finally:
        arcpy.env.workspace = prior

    if not fcs:
        raise RuntimeError(
            f"GPKG has no feature classes: {gpkg_path}"
        )
    if len(fcs) > 1:
        raise RuntimeError(
            f"GPKG has {len(fcs)} feature classes; Y2Y expects "
            f"single-layer canonical: {gpkg_path}"
        )
    return fcs[0]
