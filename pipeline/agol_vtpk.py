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


# Exact Esri-standard tiling-scheme scale values. arcpy refuses
# rounded values: "ERROR 001856: Cached scale doesn't match tiling
# scheme." The full Esri pyramid (levels 0–23) is documented at
# https://developers.arcgis.com/rest/services-reference/online/tile-map-service-specification-pre-10-9/
# Level 0 = world; level 16 = neighbourhood-scale (~1:9k). We cap
# at 16 by default — going higher would generate hundreds of MB of
# tiles for the kind of regional datasets Y2Y publishes.
_DEFAULT_MIN_CACHED_SCALE = 591_657_527.591555  # level 0
_DEFAULT_MAX_CACHED_SCALE = 9_027.977411         # level 16

_CACHE_DIR_NAME = "vtpk_cache"
_CHECKSUM_SIDECAR_SUFFIX = ".sha256"


def build_vtpk(
    gpkg_path: Path,
    dataset_id: str,
    checksum: str,
    cache_dir: Path,
    *,
    min_cached_scale: float = _DEFAULT_MIN_CACHED_SCALE,
    max_cached_scale: float = _DEFAULT_MAX_CACHED_SCALE,
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
    layer_ref = f"{gpkg_path}\\main.{layer_name}"

    # Delete any stale .vtpk at the target path — arcpy refuses to
    # overwrite by default.
    if out_path.exists():
        out_path.unlink()

    import tempfile
    with tempfile.TemporaryDirectory(prefix="y2y_vtpk_") as tmpdir:
        staging_aprx = Path(tmpdir) / "vtpk_staging.aprx"
        try:
            aprx = _open_blank_pro_project(arcpy, staging_aprx)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to create staging ArcGIS Pro project for VTPK "
                f"build: {exc}. Confirm ArcGIS Pro is installed and the "
                f"current Python is the Pro bundled environment."
            ) from exc

        maps = aprx.listMaps()
        if not maps:
            # createMap signature varies across SDK versions: try the
            # modern 'map_type' kwarg first then fall back.
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

        # Persist so CreateVectorTilePackage can see the map contents.
        aprx.save()

        try:
            arcpy.management.CreateVectorTilePackage(
                in_map=map_obj,
                output_file=str(out_path),
                service_type="ONLINE",
                tile_structure="INDEXED",
                min_cached_scale=min_cached_scale,
                max_cached_scale=max_cached_scale,
                index_polygons=None,
                summary=f"Vector Tile Package for {dataset_id}",
                tags="Y2Y",
            )
        except Exception as exc:  # pragma: no cover — arcpy-only path
            raise RuntimeError(
                f"arcpy.management.CreateVectorTilePackage failed for "
                f"{gpkg_path}: {exc}"
            ) from exc
        finally:
            # Release file handles so the TemporaryDirectory can clean
            # itself up without 'file in use' errors on Windows.
            del aprx

    if not out_path.exists():
        raise RuntimeError(
            f"arcpy reported success but no .vtpk was produced at {out_path}"
        )

    sidecar.write_text(checksum, encoding="utf-8")
    return out_path


def _open_blank_pro_project(arcpy: Any, dest_aprx: Path) -> Any:
    """Open an ArcGIS Pro project at ``dest_aprx``, seeded from Pro's
    bundled blank template.

    arcpy.mp.ArcGISProject only opens existing .aprx files — there's
    no in-memory or 'create new' constructor. So we locate Pro's
    blank template, saveACopy() it to ``dest_aprx``, then reopen
    the copy (which is what the caller mutates).

    The blank template ships at one of a few known paths depending
    on Pro version. We probe candidates in order. If none exist,
    the caller's outer try/except surfaces a clear error.
    """
    install_dir = Path(arcpy.GetInstallInfo()["InstallDir"])
    candidates = [
        install_dir / "Resources" / "ProjectTemplates" / "BlankTemplate.aptx",
        install_dir / "Resources" / "ArcCatalog" / "Templates" / "BlankTemplate.aprx",
        install_dir / "Resources" / "ApplicationTemplates" / "BlankTemplate.aprx",
    ]
    template: Path | None = next((c for c in candidates if c.exists()), None)
    if template is None:
        raise RuntimeError(
            f"Could not locate a blank ArcGIS Pro project template "
            f"under {install_dir!r}. Checked: {[str(c) for c in candidates]}. "
            f"Either Pro is not installed or this version organises "
            f"templates differently — file an issue with arcpy "
            f"version + Pro version."
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
