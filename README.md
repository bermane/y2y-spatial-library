# Y2Y Spatial Data Library

Authoritative source for finalized spatial datasets used by the Yellowstone to
Yukon Conservation Initiative (Y2Y).

## Who this is for

Primary user today: the Y2Y data steward. Eventually, Y2Y conservation and
science staff will consume datasets from here and publish select layers to
ArcGIS Online (AGOL). Everything in `library/` is considered finalized —
work-in-progress data lives elsewhere.

## The two-concerns pattern

This repository is split into two strictly separated concerns:

| Concern        | Directory    | Rule                                                    |
| -------------- | ------------ | ------------------------------------------------------- |
| **Data**       | `library/`   | Canonical spatial datasets. Never mutated by hand.      |
| **Tooling**    | `pipeline/`  | Python package that ingests, inventories, reconciles.   |

`library/` is organized by taxonomy (see below) and is the filesystem-level
source of truth for *what exists and where*. `inventory/inventory.xlsx` is the
source of truth for *metadata and history*. See [DESIGN.md](DESIGN.md) for the
full rationale (the "hybrid source of truth" model).

Supporting directories:

- `queue/` — staging for new data (`incoming/` → `processing/` → `rejected/`)
- `inventory/` — `inventory.xlsx` (generated) and `changelog.md` (append-only)
- `reports/` — timestamped reconciliation reports
- `tests/` — tests for the `pipeline` package

## Using the queue

Ingestion is **three-phase**: a lenient scan accepts any source from
the allow-list and generates a review sheet; the steward fills in
metadata and transformation declarations; an approve step transforms
the source into canonical form, validates, and promotes.

The pipeline does the conversion work — reformat, reproject,
recompress — so finalised data can arrive in any allow-listed format
(Shapefile, GeoPackage, GeoJSON, KML/KMZ, single-band GeoTIFF). The
canonical form in `library/` is always GeoPackage or Cloud Optimized
GeoTIFF in `ESRI:102008`. Phase A supports single-layer / single-band
sources only; multi-layer sources (FGDB, multi-layer GPKG) need to be
extracted with `ogr2ogr` first.

1. Drop datasets into `queue/incoming/`. Shapefile bundles
   (`.shp + .shx + .dbf + …`) all stay together.

2. **Phase 1 — scan:**

   ```
   y2y ingest
   ```

   Each accepted source bundle is moved into its own
   `queue/processing/<dataset_id>/` subdirectory; multi-layer or
   multi-band sources go to `queue/rejected/` with a `.rejected.yaml`
   sidecar; unrecognised extensions are skipped silently.
   Source provenance (`source_format`, `source_filename`, `source_crs`)
   is captured into `queue/processing/pending.xlsx`. **No format / CRS
   / naming validation runs here** — those rules describe the canonical
   *output* and run in Phase 3.

3. **Steward review:** open `pending.xlsx` and fill in:

   - **Extrinsic metadata** (required): `title` (AGOL display title),
     `summary`, `description`, `tags`, `terms_of_use`,
     `acknowledgements`, `data_steward`. Versioning is handled via
     `target_filename` (e.g. `streams_2024.gpkg`); originating-org info
     belongs in `acknowledgements`; sharing/license terms in
     `terms_of_use`.
   - **Transformation declarations**:
     - `target_filename` — auto-proposed (slugified source stem with
       canonical extension); overridable.
     - `classification` — auto-set to `vector` for vector sources;
       for raster, the steward declares `continuous` or `categorical`.
       Drives reprojection resampling and TIFF predictor.

   Sources that arrive without a valid CRS (e.g. a Shapefile missing
   its `.prj`) are **rejected at scan** — fix the source CRS
   (`gdal_edit -a_srs …` or QGIS) before re-dropping.
   - **Auto-filled overrides**: confirm or correct `category`,
     `subcategory`, `status`.

   Flip `ready` to `TRUE` on rows that are fully complete.

4. **Phase 3 — approve:**

   ```
   y2y ingest --approve
   ```

   For each ready row: pre-validate declarations → run the
   transformation (vector → GPKG, raster → COG, both reprojected to
   ESRI:102008) → run strict canonical validators on the result →
   compute the intrinsic snapshot (checksum, size, mtime, bbox) on the
   transformed file → promote to `library/<Category>/[<Subcategory>/]/`
   → append to inventory and changelog → archive the source bundle to
   `queue/archived/<dataset_id>/`. Failing rows have their `ready`
   flag reset and a `_validation_error` populated.

See [DESIGN.md §8](DESIGN.md) for the full rationale, including why a
review spreadsheet instead of a CLI prompt.

Manual drops directly into `library/` will surface as **orphans** on the next
reconciliation run. Don't do that.

## Editing, moving, and removing datasets

Once a dataset is in `library/`, three commands handle its lifecycle.
All three append a matching entry to `changelog.md`; the actor is
`$USER` by default and can be overridden with `--actor "Full Name"`.

```
y2y update <dataset_id> --set <field>=<value> [--set …]
y2y rename <dataset_id> <new-library-relative-path>
y2y tombstone <dataset_id> [--reason "…"]
y2y refresh <dataset_id>
```

- **`update`** changes non-locked fields on a row — `title`,
  `classification`, `data_steward`, `summary`, `description`, `tags`,
  `terms_of_use`, `acknowledgements`, `agol_item_id`, `notes`, plus
  `status` between `active` and `deprecated`. Locked columns
  (checksum, size, mtime, CRS, bbox, `dataset_id`, `file_path`,
  `category`, `subcategory`, `date_added`, `source_format`,
  `source_filename`, `source_crs`, `source_layer`) cannot be edited
  via `update` — they're either filesystem-derived, movement-bound,
  or part of the immutable source-provenance audit.
- **`rename`** moves a file within `library/` and updates its
  `file_path`, re-derives `category` and `subcategory` from the new
  path, and validates the new path against the naming convention and
  taxonomy. Use it for renames *and* for re-categorising a dataset.
- **`tombstone`** sets `status=tombstoned`, deletes the file from
  `library/`, and retains the row as an audit record. Irreversible;
  prompts for confirmation.
- **`refresh`** re-stats the file in `library/` and updates the
  inventory's intrinsic snapshot (`checksum_sha256`, `size_bytes`,
  `mtime`, `crs`, `geographic_extent_bbox`) to match. Use after
  editing a library file in place. Validators gate the update:
  refresh refuses to record a state where the file has lost canonical
  compliance. Mostly redundant with reconcile's auto-apply (below) —
  use it when you want to update *one* row immediately rather than
  wait for the next reconcile.

## Reconciliation

Reconciliation detects drift between `library/` and `inventory.xlsx`.
It is read-only by default and writes a timestamped markdown report
into `reports/`.

```
y2y reconcile                # fast: stat-only (size + mtime)
y2y reconcile --deep         # recompute SHA-256 for every file
y2y reconcile --fix-renames  # interactively confirm and apply detected renames
```

Findings fall into six categories:

| Category              | Meaning                                                                                              |
| --------------------- | ---------------------------------------------------------------------------------------------------- |
| **orphans**           | File in `library/` with no matching inventory row                                                    |
| **ghosts**            | Inventory row whose file is missing from disk                                                        |
| **drift**             | Path matches, content changed, **and** file is no longer canonical                                   |
| **schema violations** | File no longer satisfies format/CRS/naming, or `status=tombstoned` but file is still present         |
| **renames**           | (deep only) ghost+orphan pairs whose checksums match — the file was moved                            |
| **auto-resolved drift** | Path matches, content changed, **but** file is still canonical — snapshot auto-refreshed (informational) |

Reconcile mutates the inventory in two narrow cases (DESIGN.md §2):

- **Drift on canonical files** auto-applies via `lifecycle.refresh` —
  inventory's intrinsic snapshot updates to match the file. Logged to
  the changelog as a `refresh` entry. Pass `--no-apply-drift` to
  disable for a strict report-only run.
- **Renames** apply only with `--fix-renames`, which prompts
  `apply this rename? [y/N]` for each detected pair and applies
  confirmed ones via `lifecycle.rename`.

Other categories — orphans, ghosts (unpaired with orphans), schema
violations — surface for manual investigation; auto-fixing them is
deliberately not offered.

## Taxonomy

Nine top-level categories, drawn from `Spatial_Data_Typology.xlsx`. Disk
folder names use underscores; AGOL item titles use Title Case with spaces.

| # | Category                                    | Directory                          | Contents |
| - | ------------------------------------------- | ---------------------------------- | -------- |
| 1 | Administrative & Jurisdictional Boundaries | `Admin_Juris_Boundaries`           | Provinces, states, international boundary, First Nations lands, management units, municipal boundaries, population centers, census boundaries |
| 2 | Biodiversity & Ecosystems                   | `Biodiversity_Ecosystems`          | Ecoregions, ecosystem classification, key biodiversity areas, ecological benchmarks, biophysical data |
| 3 | Climate Resilience                          | `Climate_Resilience`               | Climate normals, projections, refugia, climate velocity, resilient landscapes, carbon stocks, fire regime, natural disturbance regime |
| 4 | Connectivity & Wildlife Movement            | `Connectivity_Wildlife_Movement`   | Corridors, linkages, resistance/permeability surfaces, telemetry, movement models, barrier analysis |
| 5 | Species & Species at Risk                   | `Species`                          | Distributions, habitat models, critical habitat, wildlife surveys, key ranges, range distribution, movement and presence data (GPS/telemetry) |
| 6 | Water                                       | `Water`                            | Hydrology, watershed boundaries, aquatic connectivity, riparian areas, water quality, fish habitat |
| 7 | Land Cover, Land Use & Disturbance          | `Land_Cover_Use_Disturbance`       | Land cover classification, change detection, NDVI/phenology, burn history, burn severity, insect/disease extent |
| 8 | Protected Areas & Conservation Lands        | `Prot_Areas_Cons_Lands`            | Parks, wilderness, WMAs, conservation easements, IPCAs, private conservation lands and easements |
| 9 | Threats, Human Footprint & Infrastructure   | `Threats_Human_Footprint_Infras`   | Roads, railways, pipelines, utility corridors, resource extraction, development, cumulative effects, human footprint indices, barrier structures, human population demographics |

> **Note:** The disk folder name `Threats_Human_Footprint_Infras` corrects a
> typo (`Footpring`) in `Spatial_Data_Typology.xlsx`. If the typology file
> is updated to match, clear this note.

### Species sub-categories

`library/Species/` is further subdivided. These come from the `SUB` block in
the typology file:

- `Caribou`
- `Elk`
- `Goat`
- `Grizzly_Bear`
- `Multi_Species`
- `Wolverine`
- `Other` — catch-all for species that don't fit the named taxa (e.g. fish, birds, single-species datasets outside the core ungulate/bear/wolverine set).

## Inventory schema

`inventory/inventory.xlsx` is generated automatically — it doesn't exist
until the first dataset is approved through `y2y ingest --approve`, at
which point a row appears for it. The schema (column groupings, types,
which fields are locked vs. overridable vs. required) is defined in
[DESIGN.md §7](DESIGN.md). Each row is appended in place; no row is
ever deleted (use `tombstone` for soft delete, which keeps the row).

## Python environment

Dependencies are declared in `pyproject.toml`: `geopandas`, `rasterio`,
`pyproj`, `fiona`, `openpyxl`, `pyyaml`, `click`, `rich`. Requires
Python 3.11+. Geospatial wheels are not yet published for Python 3.14
on PyPI; the project is developed against 3.12 (install via
`brew install python@3.12` on macOS).

```
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Run tests with `pytest`; run the CLI with `y2y` (or `python -m pipeline`).

## Status

Functional end-to-end. Two-phase ingestion (scan + approve), full
post-ingest lifecycle (update / rename / tombstone), and read-only
reconciliation with `--fix-renames` are all implemented and covered by
~90 tests. Format validators enforce the canonical-format rules in
DESIGN.md §9 (GeoPackage in `ESRI:102008` for vector; Cloud Optimized
GeoTIFF in `ESRI:102008` with ZSTD + 512×512 blocks + overviews +
NoData for raster).
