# `library/` — canonical Y2Y data store

`library/` is the on-disk home for every dataset registered in the
catalogue (`inventory/inventory.db`). Files here are **canonical** — in
the format, CRS, and naming convention required by the pipeline's
validators. Anything that doesn't conform should never be in `library/`;
it lives in `queue/` until it does.

## Layout

```
library/
├── spatial/                       # dataset_type = 'spatial'
│   ├── Biodiversity_Ecosystems/
│   ├── Climate_Resilience/
│   ├── Connectivity_Wildlife_Movement/
│   ├── Human_Dimensions/           # new in 2026 typology revision
│   ├── Juris_Political_Boundaries/
│   ├── Land_Cover_Use_Disturbance/
│   ├── Land_Designations_Tenure/
│   ├── Species/                    # only category with subcategory folders
│   │   ├── Caribou/
│   │   ├── Elk/
│   │   ├── Goat/
│   │   ├── Grizzly_Bear/
│   │   ├── Multi_Species/
│   │   ├── Other/
│   │   └── Wolverine/
│   ├── Threats_Infrastructure/
│   └── Water/
└── vtpk/                          # derived publish artifacts (Vector Tile Packages)
    └── <gpkg_file_stem>.vtpk      # one per active row with
                                   # agol_target='vector-tile-layer'
```

### `vtpk/` — derived publish artifacts

For datasets the steward wants published to AGOL as Vector Tile
Layers (rather than Feature Layers), the canonical `.gpkg` lives
in `spatial/<Category>/` as usual, AND a manually-built `.vtpk`
lives here as a sibling, flat (no category subfolders). The VTPK
filename matches the GPKG stem — e.g. `library/spatial/Land_Designations_Tenure/parks.gpkg`
pairs with `library/vtpk/parks.vtpk`.

The pipeline never writes VTPKs directly. Stewards build them
manually in ArcGIS Pro's "Share As Vector Tile Package" workflow,
drop the resulting file in `queue/incoming/`, and `y2y ingest scan`
moves it here + writes a `.sha256` sidecar. Reconcile flags VTL
rows whose VTPK is missing or stale (source GPKG newer than the
VTPK). Push uses the file here to upload + publish to AGOL.

See `DESIGN.md §15` for the full rationale (Pro upgrade fragility
made the original arcpy auto-build path untenable).

## Why `spatial/` is a level of its own

The catalogue's `datasets.dataset_type` column is currently constrained
to `'spatial'`, but the schema is shaped to support more types
(tabular CSVs, AGOL feature services, raw-imagery products, …). When a
new type joins, it gets a sibling subtree:

```
library/
├── spatial/
├── tabular/        # future
└── imagery/        # future
```

`file_path` in the catalogue is **relative to its type's subtree**, not
to `library/` directly. Resolution is `library / dataset_type /
file_path`. Code-side, `pipeline/__main__.py:_resolve_paths()` returns
`library/spatial` as the `library_root` argument that ingest /
reconcile / lifecycle take.

This restructure was applied by `pipeline/migrations/002_library_spatial_restructure.py`
on 2026-04-29. See `schema_migrations` in `inventory.db` for the audit
record.

## What lives here vs. what doesn't

In `library/spatial/`:

- One file per dataset, in canonical format (`.gpkg` for vectors, `.tif`
  COG for rasters), CRS `ESRI:102008`, lowercase-underscore filenames.
- Only files that have a corresponding row in `datasets`. Stray files
  surface as **orphans** in `y2y reconcile`.

Not in `library/`:

- `inventory/inventory.db` — the catalogue (source of truth).
- `inventory/inventory.xlsx` — generated read-only view of the
  catalogue (`y2y export-xlsx`); not the source of truth.
- `queue/` — incoming, in-flight, archived source bundles.
- `reports/` — reconciliation reports.

## How files get here

Three-phase ingestion (see `DESIGN.md §8`):

1. `y2y ingest` (Phase 1) — scan `queue/incoming/`, capture source
   metadata, stage in `queue/processing/`.
2. Steward fills required metadata in `queue/processing/pending.xlsx`,
   flips `ready=TRUE`.
3. `y2y ingest --approve` (Phase 3) — transform to canonical, validate,
   move into `library/spatial/<Category>/[<Subcategory>/]<filename>`,
   INSERT into the catalogue.

Manual drops into `library/spatial/` are **not sanctioned** — they
will surface as orphans. Use the queue.

## Operational notes

- **Renames / moves**: never `mv` files inside `library/spatial/`
  manually. Use `y2y rename <id> <new-path>` so the catalogue stays in
  sync. (`y2y reconcile --fix-renames` will offer to record a manual
  move after the fact.)
- **In-place edits**: edit the file, then run `y2y refresh <id>` so the
  catalogue's intrinsic snapshot (checksum, size, mtime, bbox)
  matches.
- **Deletion**: never `rm` from `library/spatial/`. Use
  `y2y tombstone <id>` — the row stays as audit history; the file is
  removed; reconcile will keep flagging the path forever if it
  reappears.
