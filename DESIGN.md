# Y2Y Spatial Data Library — Design

This document captures the architectural decisions behind the library so that
future sessions (and future-you) can understand *why* the scaffold looks the
way it does, not just *what* it contains. Decisions here are load-bearing —
validation, ingestion, and reconciliation logic in later sessions must honour
them.

---

## 1. Source of truth: hybrid model

Two sources of truth, each canonical in its own domain. Neither can overrule
the other where the other is authoritative.

| Question                                   | Canonical source        |
| ------------------------------------------ | ----------------------- |
| *Does this dataset exist? Where is it?*    | The filesystem (`library/spatial/`) |
| *What is its title, CRS, license, version, steward, AGOL linkage, history?* | The catalogue (`inventory/inventory.db` — SQLite) |

**Why:** Spatial datasets are first-class filesystem objects — GIS tools
open them from disk, they get copied, renamed, moved. A database as the sole
source of truth would constantly diverge from what's actually on disk.
Conversely, the filesystem can't capture provenance, licensing, or AGOL
linkage. Splitting the concern keeps each store doing what it's naturally
good at.

**Consequence:** Drift between the two is *expected* (the steward edits
files in the normal course of work) and must be *detectable* (see §2).

> **Post-migration to SQLite (2026-04-29).** The catalogue used to be
> `inventory/inventory.xlsx` — a single workbook the steward edited
> directly. As of migration 001 it is `inventory/inventory.db`, a
> SQLite database with foreign keys, CHECK constraints, and STRICT
> mode. The xlsx still exists, but only as a **regenerated read-only
> view** produced by `y2y export-xlsx`. Editing the xlsx changes
> nothing in the catalogue. See §12 for the full migration narrative,
> §13 for the PostGIS portability decisions baked into the schema,
> and §14 for the type-extensibility shape (`library/spatial/` vs
> future `library/tabular/`).

---

## 2. Reconciliation philosophy: detect, never auto-fix

`pipeline.reconcile` is read-only. It produces a timestamped report in
`reports/` and exits. The steward reads the report and decides what to do.

Reports classify findings into four categories:

| Category            | Meaning                                                              |
| ------------------- | -------------------------------------------------------------------- |
| **orphans**         | File exists in `library/` with no matching row in the inventory.     |
| **ghosts**          | Inventory row exists but the referenced file is missing on disk.     |
| **drift**           | File and row both exist and match by path, but checksum/size/mtime disagree with the recorded values. |
| **schema violations** | File still passes path/inventory checks but no longer satisfies the validators (format, CRS, naming) that admitted it. |

**Why not auto-fix:** Automatic reconciliation would silently decide whether
a missing file is a bug, an intentional removal, or a pending rename. Those
decisions belong to the steward. The cost of a false auto-heal (silently
writing bad metadata back to the inventory, or deleting a real file as an
"orphan") is much higher than the cost of the steward resolving a report.

**Narrow exceptions where reconcile mutates.** Two cases — both
unambiguously safe under canonical validation:

1. **Drift on a still-canonical file → auto-resolved.** When reconcile
   finds size/mtime/checksum drift on a row whose file still passes
   the format/CRS/naming validators, it calls
   `lifecycle.refresh` to update the inventory snapshot. The change is
   logged to the changelog as a `refresh` entry with the per-field
   diff. Listed in the report under "Auto-resolved drift" for
   visibility, not as an action item. Disable with `--no-apply-drift`
   if you need a strict report-only run.
2. **Renames** (deep mode, opt-in via `--fix-renames`). A ghost+orphan
   pair whose checksums match is unambiguously a moved file. The fix
   is steward-gated per-row — `y2y reconcile --fix-renames` prompts
   `apply this rename? [y/N]` for each candidate and applies
   confirmed ones via `lifecycle.rename`.

Every other category — orphans, ghosts (not paired with an orphan),
schema violations — is reported for manual investigation. Auto-fixing
those re-introduces the silent-decision risk above. The two exceptions
are safe because both rest on validation: drift only auto-resolves
when the file is canonical; renames only auto-fix when the steward
confirms the checksum-paired path mapping.

---

## 3. Change detection: checksums + stat signatures

Every row in the inventory records three change-detection fields:

- `checksum_sha256` — authoritative content hash
- `size_bytes` — file size at last known-good state
- `mtime` — modification timestamp at last known-good state

Two reconcile modes:

| Mode     | Check                                | Cost    | When                                |
| -------- | ------------------------------------ | ------- | ----------------------------------- |
| **fast** | `size_bytes` + `mtime` match?        | cheap   | default; run frequently             |
| **deep** | recompute SHA-256 and compare        | O(data) | weekly, or on-demand before releases |

**Why both:** SHA-256 on a multi-gigabyte raster is slow. Stat-only is fast
enough to run every day but can miss content-preserving timestamp quirks
(touch, rsync with `-t`). Deep mode is the truth; fast mode is the triage.

---

## 4. Stable dataset IDs

Every dataset receives an opaque, stable `dataset_id` at ingestion time,
independent of filename or path. All inventory rows, changelog entries, and
reconciliation reports key off `dataset_id`, not path.

**Why:** Renames and reorganizations are normal. Without a stable ID, a
rename looks like a `delete` of one dataset and an `add` of another —
history breaks at every move. With a stable ID, a rename is *(same
checksum, different path, same ID)* and the inventory simply updates the
path field while the changelog records a `rename` event.

---

## 5. Changelog is append-only

The `changelog` table in `inventory/inventory.db` is written only by
`pipeline.inventory_manager.append_changelog()`. It is never edited,
never re-sorted, never regenerated. Past rows are immutable.

The schema enforces this: every changelog row has an opaque
`cl_<ULID>` primary key and a foreign key on `dataset_id` with
`ON DELETE RESTRICT`, so accidental row removal can't orphan history.
Each row carries `timestamp`, `action`, `actor`, a free-text `note`,
and optional structured-diff columns
(`field_changed` / `old_value` / `new_value`) for callers that want to
record per-field deltas explicitly. Permitted action values are
constrained by a CHECK in `pipeline/schema.sql`.

**Why:** It's the audit trail. A changelog that can be rewritten is not an
audit trail — it's a note file. Preserving append-only semantics means that
six months from now, the steward (or an auditor, or a funder) can
reconstruct exactly what happened and when. Corrections are themselves
logged, not applied in-place.

> **Pre-migration history.** The legacy `inventory/changelog.md` file
> (the human-readable changelog as it existed before migration 001)
> has been removed. The pre-migration audit trail survives in the
> `v0.1-xlsx` git tag if it ever needs to be consulted. The exported
> `inventory.xlsx` (`y2y export-xlsx`) includes a `changelog` sheet
> rendering the SQLite table for steward readability.

---

## 6. Naming conventions

| Context              | Convention              | Example                       |
| -------------------- | ----------------------- | ----------------------------- |
| Filesystem folders   | `Title_Case_Underscores`| `Connectivity_Wildlife_Movement` |
| Filesystem files     | `snake_or_underscore_separated.ext` | `grizzly_den_sites_2024.gpkg` |
| AGOL item titles     | `Title Case With Spaces`| "Connectivity & Wildlife Movement" |
| `dataset_id`         | opaque, not human-edited| (e.g., UUID or short slug+hash) |

Metadata follows **Dublin Core+** conventions — Dublin Core core terms
(title, creator, subject, description, date, format, identifier, source,
language, rights) plus Y2Y-specific extensions (data_steward, CRS,
agol_item_id, geographic_extent, etc.).

**Why:** Underscores on disk avoid the perennial "space in filename"
problems in shell tooling, QGIS/ArcGIS project files, and cross-platform
sync (Dropbox/Parallels). Title Case in AGOL matches what end users expect
to see in the portal. Keeping these two views distinct — and mapping
between them explicitly in the inventory — means neither needs to
compromise.

---

## 7. Inventory schema

One row per dataset in the `datasets` table of
`inventory/inventory.db`. The authoritative schema is
`pipeline/schema.sql`; this section is the prose explanation of *why*
the columns are shaped the way they are. Mismatches between this
section and `schema.sql` should be resolved in favour of `schema.sql`
(it's executable; this isn't).

### Guiding principle: intrinsic vs. extrinsic metadata

The inventory stores two kinds of fields, and the distinction drives what
belongs here in the first place:

- **Intrinsic** metadata is information the spatial file already carries
  (CRS, extent, geometry type, attribute schema, feature count, band
  count). The file is the authority. The inventory does **not** try to
  mirror every intrinsic field — most are derived on demand with
  `gpd.read_file(...)`.
- **Extrinsic** metadata is authored by the steward *about* the file
  (summary, description, tags, terms of use, acknowledgements, license,
  source, version, AGOL linkage). The file has no idea about any of this,
  so it has to live outside the file. The inventory is the authority.

One narrow exception to "don't mirror intrinsic metadata": the inventory
records a **known-good snapshot** of a short list of intrinsic fields
(`crs`, `checksum_sha256`, `size_bytes`, `mtime`, `geographic_extent_bbox`).
These are not a live view — they exist so reconciliation can detect drift
when the file has changed since ingestion. Everything else intrinsic is
derived on read.

### Columns

The groupings below describe the **conceptual roles** of each column.
The schema-level column order is in `pipeline/schema.sql` and (mirrored)
`pipeline/inventory_manager.py:DATASETS_COLUMNS`. The exported
`inventory.xlsx` uses a steward-tuned ordering tuned for eye-flow
(identity → AGOL content → state → location → snapshot → spatial
properties → AGOL linkage → source provenance at the back); the
exporter's layout is in `pipeline/export_xlsx.py`.

#### Identity & location (required)

| Field         | Type              | Notes                                                                 |
| ------------- | ----------------- | --------------------------------------------------------------------- |
| `dataset_id`  | string (opaque)   | Stable across renames/moves. Primary key. Format `ds_<26-char ULID>`; the schema CHECK enforces the `ds_` prefix. Assigned at scan. |
| `dataset_type` | enum             | `'spatial'` (only value today). Reserved for future expansion to other library types — see §14. |
| `category`    | enum              | One of the 10 taxonomy categories — stored as the **display name** (full name from `Spatial_Data_Typology.xlsx`, e.g. `Jurisdictional & Political Boundaries`). Schema CHECK enforces the 10-value enum verbatim. The on-disk folder uses the underscored abbreviation (`Juris_Political_Boundaries`); the pipeline maps display↔folder. Migration 006 carried the 9→10 transition adopted at the 2026 director workshop. |
| `subcategory` | string (nullable) | Display sub-name (e.g. `Grizzly Bear`, `Multi-Species`); folder is `Grizzly_Bear` / `Multi_Species`. Only `Species` has subcategories in Phase A. Display names use **spaces or hyphens**, never underscores — the underscore is reserved for filesystem folder names. `Spatial_Data_Typology.xlsx` matches this convention; if a future revision of the typology document drifts back to underscored sub-category labels, treat it as a typo in the document, not a steward decision to change display naming. |
| `file_path`   | string (relative) | Path of the **canonical** file relative to `library/spatial/`, using folder names. Set at approve. (Pre-migration-002 the path was relative to `library/`; the relative segments did not change.) |
| `format`      | enum              | Hard enum: `'geopackage'` or `'geotiff'` (lowercase per schema CHECK; rendered with display names in the exported xlsx for steward readability). The output of transformation; admitting another canonical format would be a deliberate schema change. |

`title` is **not** a file-identity field — it's an AGOL display string
authored by the steward — so it lives in the Dublin Core+ group below
alongside `summary`, `description`, etc.

#### Source provenance (required, locked at scan)

What the file looked like *before* transformation. Captured once at scan
and never edited. Archived under the same `dataset_id` in
`queue/archived/<dataset_id>/`, so the bytes that produced any library
file remain recoverable.

| Field             | Type              | Notes                                                                       |
| ----------------- | ----------------- | --------------------------------------------------------------------------- |
| `source_format`   | enum              | `'shapefile'`, `'geopackage'`, `'geojson'`, `'kml'`, or `'geotiff'` (Phase A allow-list, lowercase per schema CHECK). |
| `source_filename` | string            | Original filename in `queue/incoming/`.                                     |
| `source_crs`      | string (auth:code or projection name) | CRS as read from the source file. **Never null** — sources without a valid CRS are rejected at scan with a "set the CRS in the source file" message. For custom CRSs without an authority code, the projection name is captured (e.g. `WGS_1984_Albers`); for those, the original WKT is recoverable from the archived `.prj`. |
| `source_layer`    | string (nullable) | Layer name within multi-layer sources. Always null in Phase A (single-layer only). |

#### Intrinsic snapshot — for drift detection only (required, computed at approve)

These fields describe the **canonical** file in `library/`, computed
from the transformation output. They are absent in `pending.xlsx` and
land in inventory only after a successful approve.

| Field                    | Type              | Notes                                                                  |
| ------------------------ | ----------------- | ---------------------------------------------------------------------- |
| `crs`                    | string (authority:code) | Canonical CRS. Always `ESRI:102008` for an admitted dataset (transformation reprojects). |
| `checksum_sha256`        | string (64 hex)   | SHA-256 of the canonical file.                                         |
| `size_bytes`             | integer           | File size in bytes at last known-good state.                           |
| `mtime`                  | ISO-8601 datetime, UTC, `Z`-suffixed | Modification time at last known-good state. |
| `geographic_extent_bbox` | string            | `minx,miny,maxx,maxy` in the canonical CRS. Snapshot only.             |
| `footprint_wkt`          | string (WKT, EPSG:4326) | Bounding-box footprint reprojected to lon/lat. Computed from `geographic_extent_bbox` at promote/refresh; intended to feed AGOL spatial-search and external preview tools without forcing them to reason about ESRI:102008. |

#### Classification (raster-only, overridable)

| Field            | Type | Notes                                                                                                     |
| ---------------- | ---- | --------------------------------------------------------------------------------------------------------- |
| `classification` | enum | `continuous` or `categorical` for raster datasets; null for vector. Steward-declared in the review sheet. Drives reprojection resampling (bilinear vs nearest), TIFF predictor (3 vs 2), and default NoData. See §11. |

#### History & governance (required)

| Field           | Type              | Notes                                                                 |
| --------------- | ----------------- | --------------------------------------------------------------------- |
| `status`        | enum              | `active`, `deprecated`, or `tombstoned` (schema CHECK). Defaults to `active` at ingestion. See below for semantics. |
| `date_added`    | ISO-8601 datetime, UTC, `Z`-suffixed | Ingestion timestamp.                       |
| `date_modified` | ISO-8601 datetime, UTC, `Z`-suffixed | Last in-place modification recorded by the pipeline. |
| `data_steward`  | string            | Person accountable for this dataset.                                  |

> **Why no `version`/`source`/`license` columns.** The library deliberately
> doesn't carry first-class version, originating-organisation, or
> license columns. Versioning is handled via `target_filename` (e.g.
> `streams_2024.gpkg` vs `streams_2025.gpkg`); originating-org info
> belongs in the free-text `acknowledgements`; sharing/licensing
> conditions belong in `terms_of_use`. Pulling these out keeps the
> AGOL-required fields tightly aligned with what AGOL renders, and
> avoids the trap where a steward populates `license` and
> `terms_of_use` redundantly with conflicting text.

**`status` semantics:**

| Value         | File on disk? | Meaning                                                                  |
| ------------- | ------------- | ------------------------------------------------------------------------ |
| `active`      | present       | Normal state. Reconciliation applies drift checks.                       |
| `deprecated`  | present       | Discouraged from new use but still available. Reconciliation still applies. |
| `tombstoned`  | absent        | Dataset has been removed. Row is retained as an audit record; `dataset_id` stays reserved. Reconciliation expects the file to be absent and flags it as a violation if it reappears. |

#### Extrinsic metadata — Dublin Core+ (required for every dataset)

Authored by the steward in the inventory and pushed to the AGOL item on
publication. **Required for every dataset in the library**, not only the
ones currently published. The library is AGOL-aligned: every dataset is
expected to reach AGOL, and the inventory must be AGOL-ready by the time
a dataset is ingested. `agol_item_id` may be null during the window
between ingestion and publication, but the descriptive fields below
must not be.

**Ingestion contract:** these fields are the gate between
`queue/processing/` and `library/`. A dataset that validates on format,
CRS, and naming but is missing any required extrinsic field is rejected
by ingestion and moved to `queue/rejected/` with the missing fields
listed in the rejection reason — it does not enter the library. This is
intentional: library membership implies AGOL-readiness, and allowing
placeholder metadata would erode that invariant over time.

| Field              | Type                    | Notes                                                                     |
| ------------------ | ----------------------- | ------------------------------------------------------------------------- |
| `title`            | string                  | AGOL item title. Title Case display string authored by the steward — *not* derived from filename. The corresponding inventory record of the *file* is `file_path`; `title` is what AGOL users see. |
| `summary`          | string (short)          | AGOL "snippet". One sentence; ~250 chars.                                 |
| `description`      | string (plain text)     | AGOL description. Plain text only — no HTML, no rich formatting.          |
| `tags`             | string (`;`-delimited)  | Flat list; not a join table. e.g., `caribou;telemetry;central_selkirks`.  |
| `terms_of_use`     | string                  | Data-sharing and privacy conditions: who may use the data, for what, with what restrictions (redistribution, attribution, sensitive-species buffering, First Nations data sovereignty, etc.). |
| `acknowledgements` | string                  | Credit line for the data author(s) and any publications to cite when using the dataset. Separate from `source` (organization of origin) and from `terms_of_use` (sharing/privacy). |

#### AGOL linkage & freeform

| Field                | Type              | Required | Notes                                                                |
| -------------------- | ----------------- | -------- | -------------------------------------------------------------------- |
| `agol_item_id`       | string            | no       | AGOL item ID once published. Null between ingestion and publication. Schema enforces uniqueness-when-not-null via a sparse `UNIQUE` index, so many unpublished rows can coexist with NULL. |
| `agol_published_at`  | ISO-8601 datetime | no       | AGOL integration (§15). When set, the timestamp the dataset was first published to AGOL. |
| `last_synced_at`     | ISO-8601 datetime | no       | AGOL integration (§15). Last successful catalogue↔AGOL sync; drives drift detection. |
| `sync_status`        | enum              | yes      | One of `clean`, `pending_push`, `pending_pull`, `conflict`, `error`, `unpublished` (schema CHECK). Defaults to `unpublished`. AGOL integration (§15). |
| `agol_format`        | enum              | no       | Publish-target intent: `feature-layer` / `vector-tile-layer` / `imagery-layer`. Pre-filled at ingest from format; editable via `y2y update`. AGOL integration (§15). |
| `internal_notes`     | string            | no       | Free-form. Renamed from the pre-migration `notes` column to make its private-audience nature explicit (the steward writes here; AGOL never sees it). Don't encode structured data; add a column instead. |

#### Spatial properties (computed at promote / refresh)

Type-aware columns the schema added at migration 001. They are
populated from the canonical file and are part of the intrinsic
snapshot — not a free-text steward field.

| Field            | Type    | Applies to | Notes                                                                              |
| ---------------- | ------- | ---------- | ---------------------------------------------------------------------------------- |
| `feature_count`  | integer | vectors    | Row count via fiona. Null for rasters.                                             |
| `raster_width`   | integer | rasters    | Pixel width. Null for vectors.                                                     |
| `raster_height`  | integer | rasters    | Pixel height. Null for vectors.                                                    |
| `pixel_size_x`   | real    | rasters    | Pixel size in CRS units (positive). Null for vectors.                              |
| `pixel_size_y`   | real    | rasters    | Pixel size in CRS units (positive; sign-flipped from the rasterio affine for north-up rasters). Null for vectors. |
| `temporal_start` | ISO-8601 datetime | both | Reserved. Datasets with explicit time coverage will populate this; null otherwise. |
| `temporal_end`   | ISO-8601 datetime | both | Reserved. As above.                                                            |

These were "intrinsic" all along — the file already knew them — but the
xlsx-era inventory didn't capture them. The SQLite schema does so they
can drive AGOL spatial search, basic catalogue browsing
(`feature_count` / pixel-size summaries), and richer reconcile reports
without the pipeline having to reopen every file on every read.

### Open questions

None currently open — prior items resolved: `status` column added
(soft-delete via `tombstoned`), checksums cover the whole bundle for
multi-file formats, extrinsic metadata is required for every dataset,
descriptions are plain text, `terms_of_use` and `acknowledgements` stay
separate with duplication accepted, type-aware spatial columns and
AGOL-reserved fields landed at migration 001.

---

## 8. Ingestion workflow: three-phase with a review spreadsheet

Ingestion is a **three-phase** process. Phase 1 is automatic and
lenient: it accepts any source from a defined allow-list and captures
provenance. Phase 2 is human review in Excel — extrinsic metadata plus
transformation declarations. Phase 3 is automatic again: it transforms
the source into canonical form, validates the result strictly, and
promotes.

The structural shift from earlier two-phase versions: source files no
longer have to arrive in canonical form. The pipeline does the
normalisation work — reformat, reproject, recompress — on the way in.
This concentrates the conversion friction in one auditable place
(every transformation gets logged to the changelog), instead of leaving
the steward to maintain ad-hoc preprocessing scripts.

### Phase 1 — Scan & stage (automatic, lenient)

Run: `y2y ingest`.

1. Walk `queue/incoming/`. For each file whose extension is in the
   source allow-list (§11), open it and run **only** the Phase-A
   acceptance checks: extension, single-layer / single-band, file
   readability. Files that pass go to processing; multi-layer or
   multi-band sources go straight to `queue/rejected/` with a reason
   file pointing at the "extract layers first" workflow. Unrecognised
   extensions are skipped silently.
2. For each accepted source, allocate a fresh `dataset_id` and create
   `queue/processing/<dataset_id>/`. Move the source bundle (the file,
   plus Shapefile sidecars when applicable) into that subdirectory.
   Per-dataset isolation lets the transformer write to a canonical
   filename without colliding with the source — even if the steward
   leaves `target_filename` identical to the source.
3. Capture source provenance into the new pending row:
   `source_format`, `source_filename`, `source_crs`, `source_layer`
   (always null in Phase A). Auto-propose `target_filename` by
   slugifying the source stem and swapping to the canonical extension
   (`.gpkg` for vector, `.tif` for raster).
4. Append the row to `queue/processing/pending.xlsx`.
5. **No format/CRS/naming validation runs in this phase.** Those rules
   describe the canonical *output*, not the source — they run in
   Phase 3 against the transformed file.
6. Do not touch `library/` or `inventory/inventory.xlsx` in this phase.

Column colouring in `pending.xlsx`:

| Colour / state | Columns                                                                  | Steward action |
| -------------- | ------------------------------------------------------------------------ | -------------- |
| Review control | `ready` (boolean, defaults to `FALSE`)                                    | Flip to `TRUE` when the row is fully filled in |
| Auto-filled, locked at scan | `dataset_id`, `source_format`, `source_filename`, `source_crs`, `source_layer`, `format`, `date_added` | None — these are forensic; editing breaks audit |
| Empty until approve, locked then | `file_path`, `crs`, `checksum_sha256`, `size_bytes`, `mtime`, `geographic_extent_bbox`, `date_modified` | None — these snapshot the *transformed* file |
| Auto-filled, overridable | `category`, `subcategory`, `status` | Confirm or correct |
| Transform inputs | `target_filename` (auto-proposed), `classification` (auto-set to `vector`; required for raster) | Confirm; supply when needed |
| Empty, required | `title`, `data_steward`, `summary`, `description`, `tags`, `terms_of_use`, `acknowledgements` | Fill in before flipping `ready` |
| Empty, optional | `agol_item_id`, `notes` | Fill in if applicable |

`ready`, `target_filename`, and `_validation_error` are pipeline-only
controls — they live in `pending.xlsx` only and are stripped when the
row is promoted.

### Phase 2 — Steward review (human, in Excel)

The steward opens `pending.xlsx`, fills required extrinsic metadata,
declares transformations (target filename, classification, optional
CRS override), confirms or corrects auto-filled overridables, and
flips `ready` to `TRUE` on rows ready to promote. They save and close.

### Phase 3 — Approve: transform → validate → snapshot → promote → archive

Run: `y2y ingest --approve`.

For each row where `ready` is `TRUE`:

1. **Pre-validate steward declarations.** Required fields non-empty,
   `category` ∈ taxonomy, subcategory rules satisfied, `status` ∈
   `{active, deprecated}`, `target_filename` passes the naming
   validator, `classification` set for raster targets, source bundle
   still present in `processing/<dataset_id>/`. Failures here cost
   nothing and surface immediately — no transformation runs.
2. **Run the transformation** (§11): vector → GPKG in `ESRI:102008`
   with geometry-type promotion; raster → COG in `ESRI:102008` with
   compression / predictor / blocksize / overviews / NoData per
   classification. Output goes to
   `processing/<dataset_id>/_canonical/<target_filename>`. Errors
   surface as `transform: <reason>` in `_validation_error`.
3. **Run strict canonical validators** on the transformed file
   (§9 — format, CRS, naming). If anything fails the canonical
   contract — say a Shapefile reproject produced an empty geometry,
   or the source had Z values that survived — the row fails with
   `post-transform: <reason>`. The transformed file is deleted; the
   source remains in processing for the steward to investigate.
4. **Compute the intrinsic snapshot** on the transformed file:
   `checksum_sha256`, `size_bytes`, `mtime`, `crs` (`ESRI:102008`),
   `geographic_extent_bbox`. Update `date_modified`.
5. **Promote.** Move the transformed file from processing to
   `library/spatial/<Category>/[<Subcategory>/]<target_filename>`.
   `INSERT` the finalised row into the `datasets` table of
   `inventory.db` (the pending-only fields `ready`, `target_filename`,
   `_validation_error` are stripped; `notes`→`internal_notes` is
   renamed; `format` and `source_format` are lowercased to the schema
   CHECK values; `dataset_type='spatial'`, `sync_status='unpublished'`
   are filled in). `INSERT` a corresponding `add` row into the
   `changelog` table that records both the source format and the
   target path.
6. **Archive the source.** Move `processing/<dataset_id>/` (minus the
   transient `_canonical/` output dir) to
   `queue/archived/<dataset_id>/`. The original bytes are preserved
   permanently; the `dataset_id` ties them to the inventory row.

Failed rows stay in `pending.xlsx` with `ready` reset to `FALSE` and
`_validation_error` populated. The steward fixes the source or the
declarations, flips `ready` back to `TRUE`, and re-runs approval.
When `pending.xlsx` has no rows left, the file is deleted.

### Why a review sheet instead of a CLI prompt

- The steward is already in a spreadsheet mindset — they can paste
  metadata from source emails, reference other rows, apply Excel
  formulas to fill in boilerplate, and review a whole batch side by
  side. A CLI prompt forces one dataset at a time with no cross-view.
- It's resumable. The steward can save and come back later; a CLI
  session cannot be paused without losing state.
- It's auditable. The `pending.xlsx` is the steward's artefact of
  record until promotion; if something looks wrong in the inventory
  later, the last-known staging sheet was saved by a human who signed
  off on it.

### Behaviour decisions

- **Persistent review sheet.** There is exactly one
  `queue/processing/pending.xlsx`. It is created on the first scan and
  reused across subsequent scans — new rows append, existing rows are
  left alone. The file is deleted only when it is empty (every row has
  been promoted or manually moved to `queue/rejected/`).
- **Per-dataset staging.** Source bundles are isolated in
  `processing/<dataset_id>/` so the transformer can name its output
  freely without clobbering source bytes, and so the whole bundle
  moves atomically to `queue/archived/<dataset_id>/` on success.
- **Source archival is permanent.** Once approved, the original bytes
  live forever in `queue/archived/<dataset_id>/`. There is no
  `--purge-archive` command yet; if archive disk usage becomes a real
  problem we'll add one with deliberate steward consent.
- **Schema evolution.** If the inventory schema gains a column between
  scans, the next `y2y ingest` run detects the mismatch against the
  existing `pending.xlsx` and adds the missing columns in place.

### Open questions for the ingestion implementation

None currently open. Multi-layer source enumeration (FGDB,
multi-layer GPKG, mixed-geometry GeoJSON) is deferred to Phase B —
see §11.

---

## 9. Canonical format standards

Every dataset in `library/` must be stored in one of the canonical
formats below. Validators in `pipeline/validators/` enforce these at
ingest time; deep reconciliation re-checks them against the file on
disk. Noncompliant data is rejected at Phase 1 (scan), before the
review sheet is generated.

### Vector

| Attribute      | Required value                                                       |
| -------------- | -------------------------------------------------------------------- |
| File format    | GeoPackage (`.gpkg`). One layer per file unless the dataset is semantically multi-layer. |
| Projection     | `ESRI:102008` (North America Albers Equal Area Conic, NAD83). Recorded as `ESRI:102008` in the inventory — never silently mapped to an EPSG approximation. |
| Geometry       | Topologically valid. Single geometry type per layer (no mixed Polygon/MultiPolygon — use MultiPolygon). 2D only unless the dataset is inherently 3D. |
| Primary key    | Every feature has a stable, non-null identifier column.              |

### Raster

| Attribute         | Required value                                                    |
| ----------------- | ----------------------------------------------------------------- |
| File format       | Cloud Optimized GeoTIFF (`.tif`).                                 |
| Projection        | `ESRI:102008`.                                                    |
| Data type         | `Float32` for continuous; `UInt8` for categorical (≤256 classes); `UInt16` for categorical with >256 classes, recorded per-dataset in the inventory. |
| Compression       | ZSTD level 9. Predictor 3 for continuous, predictor 2 for categorical. |
| NoData            | `-9999` for `Float32` continuous. `255` for `UInt8` categorical. `65535` for `UInt16` categorical. These are the defaults for AGOL/Esri interop reliability — NaN is avoided because ArcGIS stack tools have historically not always masked it correctly, and because NaN can be silently converted to real values when data round-trips through tools that don't understand it. A different sentinel is permitted only when the default would collide with valid data, and must be recorded per-dataset in the inventory. |
| Internal tiling   | 512 × 512 blocks.                                                 |
| Overviews         | Required. Generated internally (COG spec); resample method matches the category (see next row). |
| Resample method   | **Reprojection/warp:** bilinear for continuous, nearest neighbour for categorical. **Downsampling aggregation:** `average` or `cubic` for continuous, `mode` for categorical. The operation, not the default, dictates the method. |
| BigTIFF           | Auto-enabled for files >4 GB.                                     |

### Tooling compatibility note

ZSTD-compressed COGs require GDAL ≥ 2.3 and ArcGIS Pro ≥ 2.9. Older
ArcMap installs and some legacy pipelines will not open them. This is
an accepted trade-off — the compression and size wins are significant
and every target environment in Y2Y's current stack supports ZSTD —
but the constraint should be documented for any external collaborator
who receives library data.

### Open questions

- **Master snap grid.** Should Y2Y adopt a canonical pixel-alignment
  grid so datasets at the same resolution overlay pixel-for-pixel
  without per-analysis resampling? The value is real for
  cross-dataset analytical work (raster algebra, zonal stats) but the
  steward-cost is non-trivial (every incoming raster gets snapped at
  ingest, sometimes with resampling artefacts). If adopted, a small
  set of canonical resolutions (e.g., 30 m, 100 m, 1 km) with shared
  origins in `ESRI:102008` would need to be declared in this section.

---

## 10. Post-ingest lifecycle operations

Four operations on rows that are already in `inventory.db`. Each
appends a corresponding action to the changelog table.

| Command         | What it changes                                                       | Changelog action |
| --------------- | --------------------------------------------------------------------- | ---------------- |
| `y2y update`    | Non-locked fields on the row                                          | `update`         |
| `y2y rename`    | The file's path within `library/`; inventory's `file_path`, `category`, and (if applicable) `subcategory` | `rename`         |
| `y2y tombstone` | Sets `status=tombstoned` and deletes the file from `library/`         | `remove`         |
| `y2y refresh`   | Re-stats the file on disk; updates the intrinsic snapshot (`checksum_sha256`, `size_bytes`, `mtime`, `crs`, `geographic_extent_bbox`) to match | `refresh`        |

### What `update` may and may not change

`update` is for **extrinsic-metadata corrections** — fixing a typo in
the summary, refreshing a license string, switching the steward.
The admitted fields are deliberately narrow:

- **Allowed:** `title`, `data_steward`, `summary`, `description`,
  `tags`, `terms_of_use`, `acknowledgements`, `agol_item_id`,
  `internal_notes`, `classification`, plus `status` transitions
  between `active` and `deprecated`.
- **Rejected as locked** (filesystem-derived snapshot fields, §3):
  `dataset_id`, `checksum_sha256`, `size_bytes`, `mtime`, `crs`,
  `geographic_extent_bbox`, `date_added`. Editing these in the
  inventory would defeat drift detection.
- **Rejected as movement-bound:** `file_path`, `category`,
  `subcategory`. Changing any of these requires also moving the file
  on disk — use `rename`.
- **`status=tombstoned` rejected:** tombstoning has filesystem side
  effects, so it goes through its own command. There is no path back
  from `tombstoned`; un-tombstoning is intentionally not implemented.

If `update` is called with values identical to the current ones, it
is a no-op — no inventory rewrite, no changelog entry.

### Rename: active vs. record-only

`rename` handles three filesystem situations:

| Old path on disk? | New path on disk? | Behaviour                                                                   |
| :---------------: | :---------------: | --------------------------------------------------------------------------- |
| yes               | no                | Active move: pipeline `os.rename`s the file, then updates inventory.        |
| no                | yes               | Record-only: file was moved manually; pipeline updates inventory only. This is the path `y2y reconcile --fix-renames` uses. |
| yes               | yes               | Conflict — error. Resolve manually.                                         |
| no                | no                | Error — no file to operate on.                                              |

The new path's filename must pass the naming validator; its category
(and subcategory if applicable) must satisfy the taxonomy. `rename`
is therefore also the way to **re-categorise** a dataset — point it
at a path under a different `<Category>/` folder and the inventory's
`category` field follows.

### Refresh: accept canonical drift after editing in place

Editing a library file directly (adding a vector field, recomputing
attributes, regenerating overviews) leaves the inventory's intrinsic
snapshot stale. `refresh` is the explicit "accept the new state"
operation: it re-stats the file, recomputes the snapshot, runs the
canonical validators, and writes a `refresh` changelog entry with the
per-field diff.

Two important properties:

- **Validation gates the update.** If the in-place edit broke
  canonical compliance (added Z, wrong CRS, mixed geometry, dropped
  ZSTD, etc.), `refresh` errors and the inventory snapshot stays old.
  The steward fixes the file and re-runs.
- **It's the only operation that touches locked snapshot columns**,
  and only by recomputing from the file — never from arbitrary
  steward input.

In practice the steward rarely runs this manually: `y2y reconcile`
auto-applies refresh to drift findings whose files pass validators
(see §2). The CLI is there for one-off use after an in-place edit
when you don't want to wait for the next reconcile cycle.

### Tombstone is irreversible by design

`tombstone` deletes the file from `library/` and sets
`status=tombstoned`. The `dataset_id` stays reserved forever —
inventory rows are never deleted; the row remains as an audit record.
The CLI prompts for confirmation; bypass with `--yes` if scripted.

If the file is already absent (e.g., the steward deleted it
manually), `tombstone` proceeds anyway and records that fact in the
changelog. This is the only operation that *can* converge a
file-already-deleted situation back into agreement without
`--fix-…` intervention; every other inventory-vs-disk divergence is
surfaced by reconcile and resolved by the steward through `update` /
`rename` / file-system action.

#### Hard-delete exception (pre-production only)

While the catalogue is still pre-production, the steward may
**hard-delete** dataset rows (and their changelog history) instead of
tombstoning. This is the path used by migrations 004 (re-ingest the
mis-classified GB rasters) and 005 (purge Phase 6 verification
artifacts). The mechanics:

1. The exception is invoked **only via a numbered migration script**,
   never from the CLI. Each invocation is auditable in
   `pipeline/migrations/` and recorded in `schema_migrations`.
2. The migration disables FK enforcement (`PRAGMA foreign_keys =
   OFF`), `DELETE`s the targeted changelog rows, `DELETE`s the
   dataset rows, runs `PRAGMA foreign_key_check` while still off to
   verify no other history is orphaned, then re-enables FKs.
3. Pre-flight safety: the migration refuses to run if any target row
   isn't already in the expected state (e.g., 005 refuses if either
   target isn't `status='tombstoned'`).

**This exception is retired at production.** Once the project crosses
into production use the policy reverts to "tombstone, never delete":
the FK constraint stays enforced, and the only way to remove a
dataset from active operation is `y2y tombstone`. Hard-deletes from
the production catalogue would destroy audit history that funders,
auditors, and successor stewards depend on.

The pattern exists because pre-production accumulates verification
scaffolding (test ingests, classifications that turned out wrong)
whose audit trail genuinely isn't worth keeping. After production,
every dataset's history matters.

### Actor resolution

Every lifecycle command writes a changelog entry with an `actor:`
field. The CLI resolves it in this order: `--actor` flag if provided,
else `$USER`, else `$USERNAME`, else the literal string `"unknown"`.

---

## 11. Source-format admission and transformation rules

This section defines the **source allow-list** — what scan accepts —
and the **transformation rules** that turn an admitted source into a
canonical-form file. Together they answer "what can the steward drop
into `queue/incoming/` and what does the pipeline do with it?"

### Allow-list (Phase A)

| Class  | Accepted extensions                                  | Notes                                                |
| ------ | ---------------------------------------------------- | ---------------------------------------------------- |
| Vector | `.shp` (with sidecars), `.gpkg`, `.geojson`/`.json`, `.kml`/`.kmz` | Single-layer only. Mixed-geometry GeoJSON rejected. |
| Raster | `.tif`/`.tiff`                                       | Single-band only. Other raster formats not admitted. |

Anything outside the allow-list is **silently skipped at scan** (we
don't reject — the steward might intentionally have unrelated files in
incoming). Multi-layer / multi-band sources within the allow-list are
**rejected with a reason**, since they're recognised as spatial but
not yet supported.

### Phase A boundaries

Out of scope until a follow-up session:

- **Multi-layer source enumeration** (FGDB, multi-layer GPKG,
  mixed-geometry GeoJSON producing one row per layer/type). Phase A
  rejects these at scan with a message pointing at `ogr2ogr` for
  manual extraction.
- **Other raster formats** (`.img`, `.vrt`, `.nc`, `.h5`). Convert to
  GeoTIFF first.
- **Geometry repair.** A self-intersecting polygon is rejected; the
  steward fixes the source.
- **dtype coercion.** A raster with `int32` is rejected; the steward
  decides explicitly what dtype to convert to before re-ingesting.
- **Z/M dimension stripping.** A 3D vector layer is rejected; the
  steward decides whether to drop the dimension or keep it (and pause
  ingestion until 3D support is real).

### Passthrough optimisation

Both transformers (vector and raster) short-circuit when the source is
**already canonical** — i.e., the source file by itself passes the
canonical format and CRS validators that would normally run on the
*output*. In that case the transformation is just `shutil.copy2` —
no re-read, no warp, no re-encode, byte-identical destination.

This is the common case for Y2Y-internal datasets that are already
produced in canonical form (e.g. a `.gpkg` already in `ESRI:102008` with
valid 2D geometry, or a 512×512 ZSTD COG already at the right
predictor/dtype/NoData). For a 1 GB raster, the difference is a
sub-second copy versus a ~minute reproject + re-encode.

The naming convention is checked separately against `target_filename`
in `approve()`, so passthrough still supports renaming
(`Source_Name.gpkg` → `source_name_v2.gpkg`).

For raster, an additional consistency check at passthrough: the source
file's predictor must agree with what `classification` implies (3 for
`continuous`, 2 for `categorical`). A mismatch is rejected — it means
the steward declared a classification that doesn't match the source's
dtype.

### Transformation: what the pipeline *does* do automatically (when transformation is needed)

#### Vector

Source format → `GeoPackage` in `ESRI:102008`, single concrete
geometry type, valid geometries.

| Transformation               | Behaviour                                                                                              |
| ---------------------------- | ------------------------------------------------------------------------------------------------------ |
| Format conversion            | Any allow-listed vector format → `.gpkg` via geopandas/fiona.                                          |
| Reprojection                 | Always to `ESRI:102008`. No-op if source is already there.                                             |
| Source CRS missing           | **Rejected at scan** — steward must add a `.prj` (or otherwise set the CRS) before re-dropping. The transformation path never sees a CRS-less file. |
| Mixed-geometry promotion     | `Polygon` + `MultiPolygon` → `MultiPolygon`. Same for LineString and Point pairs. Other mixes error.   |
| Invalid geometry             | Rejected with the offending feature index and shapely's `explain_validity` reason. No auto-repair.     |
| Z/M dimensions               | Rejected.                                                                                              |
| Empty source                 | Rejected (zero features).                                                                              |

#### Raster

Source `.tif`/`.tiff` → `Cloud Optimized GeoTIFF` in `ESRI:102008`,
single-band, with the canonical compression / predictor / blocksize /
overviews / NoData profile from §9.

| Transformation        | Behaviour                                                                                                        |
| --------------------- | ---------------------------------------------------------------------------------------------------------------- |
| Format profile        | GTiff driver, ZSTD level 9, internal 512×512 tiles, internal overviews, BigTIFF auto-on.                         |
| Reprojection          | Always to `ESRI:102008`. Resampling = bilinear (continuous) / nearest (categorical), driven by `classification`. |
| Predictor             | 3 for `continuous` (Float32), 2 for `categorical` (UInt8/UInt16). Driven by `classification`.                    |
| Source CRS missing    | **Rejected at scan** — steward sets the CRS in the source (`gdal_edit -a_srs …`) before re-dropping.             |
| dtype                 | Source must already be `float32`, `uint8`, or `uint16`. Other dtypes rejected.                                   |
| NoData                | Source's NoData if defined; otherwise canonical default by dtype (`-9999.0` / `255` / `65535`).                  |
| Multi-band            | Rejected at scan; never reaches transformation.                                                                  |
| Classification missing | Rejected at pre-transform validation.                                                                            |

### Why classification has to be steward-declared

Continuous vs. categorical is a property of *what the values mean*,
not what's in the file. A 0-255 raster could be a soil-pH map (continuous)
or a land-cover code raster (categorical) with identical bytes on disk.
The wrong choice produces silently wrong results — bilinear-resampling a
land-cover code creates impossible mid-class values; nearest-resampling
a continuous variable produces a stair-step artefact. So the steward
declares it once in the review sheet, and the pipeline applies the
correct resampling and predictor for the lifetime of the dataset.

### Why no `source_crs_override`

A previous design carried a steward-editable `source_crs_override`
column for files that arrived without a CRS, or with a wrong one
embedded. We cut it: it's strictly cleaner to require sources to
arrive with a valid CRS already set.

Reasoning:

- Setting a CRS in the source file is a one-time fix the steward does
  once outside the pipeline (a `gdal_edit -a_srs` invocation, or
  reprojecting in QGIS, or producing the file with the right CRS in
  the first place). Doing it in-place on the source file means the
  next person who opens that source — even outside this library —
  also sees the correct CRS.
- The override-column path conflated two things: (a) "the source
  doesn't have a CRS" (a fixable upstream defect) and (b) "the source
  has a wrong CRS that needs replacing without warping" (a rare
  steward-asserted correction). Better to ask the steward to fix
  these in the source file rather than carry a rarely-used column.
- One column instead of two means less ambiguity about where the
  authoritative source CRS lives.

Practical consequence: a Shapefile without a `.prj`, or a TIFF
without GeoTIFF CRS tags, or a GeoJSON exported sans CRS, all
**reject at scan** with a clear "set the CRS in the source" message.

### Source archival

After successful approval, `processing/<dataset_id>/` (minus the
transient `_canonical/` output dir) is moved verbatim to
`queue/archived/<dataset_id>/`. The Shapefile sidecars all travel
with their `.shp`. The directory name is the `dataset_id`, so:

- The library file's `dataset_id` ties it to its archive.
- A future "what was the original?" question is answerable in two
  filesystem hops: read inventory row → look in archive subdirectory.

There is no automatic purge. If a dataset is later tombstoned, its
archive stays — that's part of the audit trail.

---

## 12. SQLite catalogue: source of truth

The catalogue lives in `inventory/inventory.db`, a single SQLite file.
This section explains why we chose SQLite over the previous
xlsx-as-source-of-truth design, what guarantees the schema gives, and
how the legacy xlsx fits into the post-migration world.

### Why SQLite, not xlsx

The xlsx era was simple and worked fine for a 20-row catalogue
maintained by one steward. It scaled poorly along three axes:

1. **No type or referential enforcement.** A typo in `format`
   ("GeoPackge"), a numeric column accidentally stored as text, a
   changelog entry referencing a non-existent `dataset_id` — all of
   these slipped past Excel and surfaced later as inconsistent
   reconcile reports or AGOL-publication failures.
2. **No transactional guarantees.** A pipeline run that crashed
   between writing the inventory sheet and the changelog left the two
   permanently out of sync. Excel's lock-file race made this worse:
   the steward could open the workbook between the pipeline's read
   and write, then save over the pipeline's pending changes.
3. **Single-writer.** The xlsx is a binary format that's effectively
   single-writer; concurrent edits aren't a thing. SQLite's WAL mode
   lets a long-running reader (the steward scrolling the export)
   coexist with pipeline writers.

SQLite addresses all three:

- **Type enforcement.** Every table is declared `STRICT` (SQLite ≥
  3.37). Inserting `'GeoPackge'` into a column with a CHECK on
  `('geopackage', 'geotiff')` is a hard error at the SQL boundary, not
  a future debugging session. Foreign keys prevent dangling
  changelog entries.
- **Transactions.** Every catalogue mutation in `pipeline/` is wrapped
  in a single `with conn:` block; partial failures roll back. The
  ingest's "transformed file moved + datasets row inserted +
  changelog row inserted" trio either all happen or none do.
- **Steward UX preserved.** The exported `inventory.xlsx` (`y2y
  export-xlsx`) is a faithful read-only render of the catalogue; the
  steward keeps their familiar grid view. They just can't edit it
  back into the system, which is the entire point — edits go through
  CLI commands that write proper changelog rows.

### What lives in the database

Three tables:

| Table              | Purpose                                                                              |
| ------------------ | ------------------------------------------------------------------------------------ |
| `datasets`         | One row per dataset. The catalogue. Schema in §7.                                    |
| `changelog`        | Append-only audit log (§5). FK on `dataset_id` with `ON DELETE RESTRICT`.            |
| `schema_migrations`| Tracks which numbered migrations have been applied. One row per applied migration.   |

Migrations are numbered scripts under `pipeline/migrations/`. They are
**append-only** — once a migration has been applied to a real
database, the script must not be edited (a corrected migration is a
new file, not an amendment to the old one). Each migration records
its version in `schema_migrations` on success.

Two migrations exist today:

- **001** — bootstrap: convert the legacy `inventory.xlsx` into
  `inventory.db`, re-key every dataset from `ds_<12hex>` to
  `ds_<26-char ULID>`, backfill spatial-properties columns, write a
  per-dataset `migrated_from_xlsx` + `id_format_migration` pair into
  the changelog, rename the xlsx to
  `inventory.xlsx.pre-migration-backup`.
- **002** — library restructure: move `library/<Category>/...` →
  `library/spatial/<Category>/...` so future dataset types can occupy
  sibling subtrees (§14). No `file_path` updates needed; the schema's
  comment specifies the path is relative to `library/spatial/` so
  pre-existing values were already correct relative to the new typed
  root.

### Connection conventions

`pipeline/db.py:get_connection()` is the only sanctioned way to open
the catalogue. It applies, on every connection:

- `PRAGMA foreign_keys = ON` — the schema's FKs are advisory at
  runtime without this. Easy to forget, expensive when you do.
- `PRAGMA journal_mode = WAL` — readers don't block writers and vice
  versa. Set once on disk; subsequent connections inherit.
- `row_factory = sqlite3.Row` — rows behave like dicts so call sites
  can use `row["dataset_id"]` instead of positional indexing.

### The xlsx is a regenerated view

`y2y export-xlsx` produces a 2-sheet workbook:

- **`inventory`** sheet — every column from `datasets`, in a layout
  tuned for steward eye-flow (`pipeline/export_xlsx.py`).
- **`changelog`** sheet — every row from the changelog table, oldest
  first.

Re-export overwrites the file. The exporter refuses to write when
Excel has the xlsx open (the same `~$inventory.xlsx` lock-file check
the pre-migration code used). The xlsx is **not** the source of
truth and should be regarded as a derived artifact like a build
output: regenerate freely, never edit.

The legacy `inventory/changelog.md` is left on disk as historical
reference but no longer written to. The pre-migration xlsx is kept
locally as `inventory.xlsx.pre-migration-backup` (gitignored; the
canonical pre-migration snapshot is the `v0.1-xlsx` git tag).

---

## 13. PostGIS portability

The schema is designed so that promoting from SQLite to PostGIS in a
later session is a deliberate, scoped change — not a rewrite. The
constraints that make this possible are baked into `schema.sql` from
day one.

### Identifier conventions

- **Lowercase, underscore-separated identifiers.** No quoted
  identifiers, no mixed-case columns. Case-insensitive in SQLite,
  case-sensitive when quoted in Postgres; sticking to lowercase means
  identifiers behave the same way under both engines without
  per-engine quoting.
- **No reserved words.** `dataset_type`, `internal_notes`, etc. — not
  `type`, not `notes`, not `description` adjacent to anything reserved.

### Type discipline

- **`TEXT` for all strings.** No `VARCHAR(n)`. Postgres's `TEXT` and
  `VARCHAR(n)` are functionally equivalent except for the (rarely
  desirable) length cap; SQLite ignores `VARCHAR` length entirely. A
  `TEXT`-only schema is identical-shape under both.
- **`INTEGER` for booleans.** SQLite has no native `BOOLEAN`; Postgres
  does, and exposes it as a separate type. We store 0/1 in `INTEGER`
  columns (`agol_item_id IS NOT NULL` is the closest current example
  of a derived boolean) — promotable to `BOOLEAN` in the Postgres
  port with a one-line cast.
- **`REAL` for floats** — pragmatic alias for `DOUBLE PRECISION` under
  Postgres.
- **No SQLite-specific types.** No `BLOB` (we don't store binary
  payloads), no SQLite affinity-only types (`NUMERIC`).

### `STRICT` mode

Every table is declared `... STRICT;` so SQLite enforces the column
types as declared. Pre-3.37 SQLite was famously promiscuous about
types; STRICT brings type-checking up to Postgres parity for the
columns the schema cares about. Without STRICT, an `INTEGER NOT NULL`
column would silently accept the string `"1186475843"` from an
xlsx-importing path.

### Timestamps

ISO-8601 UTC strings with explicit `Z` suffix
(`2026-04-29T21:31:33Z`), stored in `TEXT` columns. **Why strings,
not native timestamp types:**

- SQLite has no native datetime type — its `DATETIME` is just `TEXT`
  with parsing functions on top.
- Postgres has `TIMESTAMPTZ`, but a string column with a
  parseable-by-everything format ports cleanly: `ALTER TABLE ...
  ALTER COLUMN ... TYPE TIMESTAMPTZ USING ...::TIMESTAMPTZ` does the
  conversion in one step.
- Lots of consumers (AGOL, Excel, jq scripts) prefer ISO strings to
  whatever each engine's native format is.

The explicit `Z` is non-negotiable: a bare `2026-04-29T21:31:33` is
ambiguous about timezone, and "we always mean UTC" is not enforceable
without the suffix. Migration 001 upgraded the xlsx-era bare-date
values (`2026-04-27`) to `2026-04-27T00:00:00Z` for this reason.

### Spatial columns: WKT, not native geometry

`footprint_wkt` is stored as a `TEXT` Well-Known-Text string in
EPSG:4326, not as a SQLite-spatialite or Postgres `geometry` type.

- Keeps SQLite portable without forcing a Spatialite extension on
  every install.
- The Postgres port can read the column as `TEXT` and either keep it
  that way or cast to `geometry(Polygon, 4326)` via
  `ST_GeomFromText`. The choice is local to the Postgres deployment,
  not constrained by the schema.

`geographic_extent_bbox` is a `'minx,miny,maxx,maxy'`-as-string field
in the canonical CRS (`ESRI:102008`), kept for drift detection
(reconcile compares string-equal). `footprint_wkt` is the EPSG:4326
analogue intended for AGOL/external consumers that don't speak
ESRI:102008.

### Foreign keys + RESTRICT

`changelog.dataset_id` references `datasets.dataset_id` with `ON
DELETE RESTRICT`, so accidental dataset removal can't orphan audit
history. Postgres enforces FKs by default; SQLite needs `PRAGMA
foreign_keys = ON` per connection (handled in `pipeline/db.py`).

### Sparse `UNIQUE` indexes

`agol_item_id` is `UNIQUE`-when-not-null via a partial index:

```sql
CREATE UNIQUE INDEX idx_datasets_agol_item_id_unique
    ON datasets(agol_item_id) WHERE agol_item_id IS NOT NULL;
```

So many unpublished rows (NULL `agol_item_id`) coexist while every
*published* row's item ID is unique. Postgres has identical syntax
for partial indexes, so the port is mechanical.

### What's *not* PostGIS-portable yet

- **`PRIMARY KEY` is opaque text.** Both engines support that
  natively; no concern.
- **`AUTOINCREMENT` is not used anywhere.** Every PK is a ULID-bearing
  text column generated by the application. No SQLite-specific
  auto-id semantics to port.
- **No `WITHOUT ROWID` tables.** They're SQLite-only and we don't
  need them.

The above is sufficient that a Postgres port is a well-defined task
("translate `STRICT` to native typing, add a Spatialite/PostGIS
preference for `footprint_wkt` if desired, replicate the partial
index"), not an architectural rewrite.

---

## 14. Type extensibility

The catalogue is shaped to grow beyond spatial data. Today
`datasets.dataset_type` is constrained to `'spatial'`; tomorrow it
might admit `'tabular'`, `'imagery'`, or document-class types. The
schema is structured so that growth doesn't require a teardown.

### Two-tier shape

The current layout is a deliberate **single-table-with-sentinel**
pattern:

```
datasets
├── [type-agnostic columns]   # identity, taxonomy, extrinsic metadata,
│                               lifecycle, AGOL linkage
└── [spatial-specific columns] # CRS, bbox, footprint_wkt, raster_*,
                                 feature_count, source_format, …
```

The schema's source comments mark each column group with
`[type-agnostic]` or `[spatial-specific]` so the boundary is
explicit. This is not the long-term shape — it's the **pragmatic
shape for one type**.

When a second type appears, the spatial-specific columns move to a
per-type extension table:

```
datasets                       # unchanged: type-agnostic only
├── dataset_id (PK)
├── dataset_type
├── title, category, …

spatial_datasets               # new: spatial-specific columns
├── dataset_id (PK + FK)
├── crs, geographic_extent_bbox, footprint_wkt, …

tabular_datasets               # new
├── dataset_id (PK + FK)
├── row_count, column_schema_json, …
```

The migration is a numbered script (`003_split_spatial_extension.py`
or similar) that:

1. Creates the extension tables.
2. Copies spatial-specific columns from `datasets` into
   `spatial_datasets`.
3. Drops those columns from `datasets`.
4. Adjusts the application's read path to JOIN by `dataset_type`.

Until that migration runs, the spatial-specific columns sit on
`datasets` and nothing breaks. The single-table pattern is fine for
one type; the extension-table pattern is right when there are two or
more.

### Filesystem layout matches

`library/` mirrors the same shape:

```
library/
├── spatial/                 # dataset_type = 'spatial'
│   └── <Category>/...
├── tabular/                 # future: dataset_type = 'tabular'
└── imagery/                 # future: dataset_type = 'imagery'
```

`file_path` in the catalogue is **relative to its type's subtree**.
Resolution is `library / dataset_type / file_path`. The CLI's
`_resolve_paths` returns `library/spatial/` as the spatial library
root; a future tabular CLI would resolve to `library/tabular/` for
that type's commands. There is no top-level "library root" the
pipeline operates against in an undifferentiated way; type is always
selected first.

### What stays type-agnostic

Type expansion does not change:

- Reconciliation philosophy (§2). A tabular orphan is still an
  orphan; a tabular ghost is still a ghost. Reconcile becomes
  type-aware (it walks the right subtree per type) but its findings
  taxonomy is unchanged.
- Stable IDs (§4). `ds_<ULID>` is type-agnostic; reading a
  `dataset_type='tabular'` row gives you the table to JOIN to (or the
  columns to read directly while still single-table).
- Append-only changelog (§5). One changelog table, all dataset types.
  An `add` of a tabular dataset and an `add` of a spatial dataset
  look structurally identical in the audit log.
- Three-phase ingestion (§8). Each type has its own scan validators
  and transformer (vector / raster / tabular / …) but the queue →
  pending.xlsx → approve flow is uniform.

### Why not "just use AGOL's classification"

AGOL distinguishes feature services, tile services, hosted feature
layers, etc. — those are *publication* shapes, not *data type*
shapes. A spatial-data row in our catalogue might publish as any of
several AGOL item types depending on size and use case. The
catalogue's `dataset_type` is about what the data **is**;
publication shape is downstream and lives in `agol_item_id` linkage.

---

## 15. AGOL integration

The catalogue ↔ ArcGIS Online bridge is **implemented** in
`pipeline/agol_sync.py` (+ `agol_config.py`, `agol_thumbnails.py`,
`agol_vtpk.py`) and the `y2y agol-sync` CLI sub-group. It publishes
the catalogue's authoritative datasets to the Y2Y Conservation Atlas
org and keeps catalogue ↔ AGOL state reconciled bidirectionally.

### State columns

| Column                | Default        | Meaning                                                                            |
| --------------------- | -------------- | ---------------------------------------------------------------------------------- |
| `agol_item_id`        | NULL           | The AGOL **service** item ID once published. Sparse UNIQUE — one published item per row. |
| `agol_published_at`   | NULL           | Timestamp the dataset was first published to AGOL. Set once, then immutable.       |
| `last_synced_at`      | NULL           | Timestamp of the last successful catalogue ↔ AGOL sync. Drives drift detection.    |
| `sync_status`         | `unpublished`  | One of `clean`, `pending_push`, `pending_pull`, `conflict`, `error`, `unpublished`. Schema CHECK. |
| `agol_format`         | per-format     | Publish-target intent: `feature-layer` / `vector-tile-layer` / `imagery-layer`. Pre-filled at ingest from format; editable via `y2y update`. |

### `sync_status` state machine

```
unpublished ──(push succeeds)──────────────> clean
unpublished ──(adopt: AGOL matches)────────> clean
unpublished ──(adopt: AGOL drifted)────────> conflict
clean ──(catalogue edit, auto-marked)──────> pending_push
clean ──(reconcile detects AGOL drift)─────> pending_pull
clean ──(pull --no-resolution, drift)──────> conflict
pending_push ──(push / reconcile succeeds)─> clean
pending_push ──(push / reconcile fails)────> error
pending_pull ──(pull --accept | --reject)──> clean
pending_pull ──(pull --no-resolution)──────> conflict
conflict ──(pull --accept)─────────────────> clean
conflict ──(pull --reject)──> pending_push ─> clean
error ──(reconcile retry succeeds)─────────> clean
error ──(reconcile retry fails)────────────> error
any-with-item ──(unpublish)────────────────> unpublished
```

The state machine lives in application code (`agol_sync.py`), not the
schema — SQLite CHECK enforces the **value** of `sync_status`, not the
legal transitions. Every transition writes a `metadata` changelog
entry, so catalogue mutations and AGOL syncs interleave in one audit
trail by `timestamp`.

### Catalogue-centric scope

Every operation iterates rows in `datasets`; the org's full content
tree is never scanned. Consequences:

- **In catalogue, not on AGOL** (`agol_item_id IS NULL`) → push candidate.
- **In both** (linked rows) → reconciled bidirectionally.
- **On AGOL, not in catalogue** (maps, dashboards, the steward's
  experiments) → **ignored**. The integration never ingests or
  classifies untracked AGOL items.
- **Tombstoned rows** → filtered out of every iteration. Call
  `y2y agol-sync unpublish <id>` before tombstoning if the AGOL item
  should go too.

Integration-created items are stamped with `typeKeywords` (`Y2Y`,
`Y2Y:dataset_id:<id>`, `Y2Y:category:<category>`) so a future audit
tool can find them — no v1 command consumes these.

### Per-row AGOL footprint: source + service

Vectors and vector-tile layers produce **two** AGOL items:

- **Source** in `_sources/` — private, minimal metadata, the uploaded
  GPKG/VTPK. Carries `Y2Y:source` (+ `Y2Y:vtpk_sha256:<hex>` for VTL).
- **Service** in `<Category>/` — public (org + Conservation Atlas
  group), full metadata + thumbnail. This is the `agol_item_id`.

Linked via `Service2Data`. `unpublish` walks that link to delete both.

**Imagery is the exception** — published via
`arcgis.raster.publish_hosted_imagery_layer` in a **no-source model**:
no separate source TIFF item exists. Refreshing imagery re-runs the
hosted-imagery publish.

### The three publish targets (`agol_format`)

| `agol_format`       | Source format | Path |
| ------------------- | ------------- | ---- |
| `feature-layer`     | geopackage    | Upload GPKG → `publish()` → hosted Feature Service. Data refresh via `FeatureLayerCollection.overwrite()`. |
| `imagery-layer`     | geotiff       | `publish_hosted_imagery_layer` (no-source). |
| `vector-tile-layer` | geopackage    | **Manual VTPK** (steward builds in Pro UI, drops in `queue/incoming/`, `y2y ingest scan` files it to `library/vtpk/`); pipeline uploads VTPK → `publish()` → Vector Tile Service. No arcpy in the pipeline. See the VTPK lifecycle below. |

Switching a row's `agol_format` (e.g. `feature-layer` →
`vector-tile-layer`) makes the next push delete the old AGOL
representation (`_handle_target_switch`) before publishing the new one
— credit savings is the whole point of VTL, so the old FS doesn't linger.

#### VTPK lifecycle (vector-tile-layer)

The pipeline never imports arcpy. The steward builds the `.vtpk` once
per source-data change in ArcGIS Pro's "Share As Vector Tile Package"
dialog, drops it in `queue/incoming/`, and `y2y ingest scan` dispatches
`.vtpk` files to `agol_vtpk.ingest_one_vtpk` — matched to the catalogue
row by file stem, moved to `library/vtpk/<stem>.vtpk` with a `.sha256`
sidecar. If a GPKG and its same-stem VTPK arrive together, scan pairs
them and approve promotes both. `y2y reconcile` checks the VTPK
invariant on every run: **missing-VTPK** (VTL row with no `.vtpk`),
**stale-VTPK** (GPKG newer than the `.vtpk`), **orphan-VTPK** (`.vtpk`
on disk with no matching VTL row).

### Auto-sync (default on)

Catalogue mutations propagate to AGOL automatically:

- `inventory_manager._maybe_mark_dirty()` flips `clean → pending_push`
  on every `y2y update` / `rename` / `refresh` and on new ingest
  approvals.
- `agol_sync.try_auto_push()` then attempts a best-effort push when
  `Y2Y_AGOL_AUTO_PUSH` is true (the env-var default). **Any** AGOL
  failure — no profile, offline, 5xx — is swallowed and audited in
  the changelog; the catalogue mutation always succeeds independent of
  AGOL state. The row stays `pending_push` for the next reconcile.

Set `Y2Y_AGOL_AUTO_PUSH=false` to make all pushes manual.

### Bidirectional reconcile

`y2y agol-sync reconcile [--dry-run]` iterates every active row:

- `pending_push` → push (failures → `error`).
- `clean` → compare AGOL's `modified` timestamp to `last_synced_at`;
  if AGOL is newer, flag `pending_pull`.
- `error` → retry push once.
- `unpublished` / `pending_pull` / `conflict` → skipped (steward's call).

Writes a markdown report to `reports/agol_reconcile_<ts>.md`. Intended
for a weekly schedule — see "Scheduling" below.

### Pull + conflict resolution

`y2y agol-sync pull <id> [--accept | --reject]`:

- **no flag** → fetch + diff + mark `conflict`, log the per-field diff.
- **--accept** → catalogue absorbs AGOL's text fields (title, summary,
  description, tags, acknowledgements, terms_of_use). `categories` is
  filesystem-bound and skipped with an `internal_notes` note — change
  it via `y2y rename`.
- **--reject** → re-push the catalogue to overwrite AGOL drift.

`pull --all-pending` surfaces diffs for every `pending_pull` row
without auto-resolving. Drift detection follows the same
"never auto-fix divergence" philosophy as `y2y reconcile` (§2).

### Diff normalisation

Field comparison treats representational differences as non-drift:

- **HTML-permitting fields** (description, accessInformation,
  licenseInfo) compare semantic-equivalence — AGOL wraps plain text in
  `<p>…</p>` on save, and that structural HTML is stripped before
  comparing. Meaningful tags (`<a href>`, `<ul>`, …) the plain
  catalogue can't represent **do** count as drift. Plain text remains
  the catalogue norm; raw HTML in a catalogue column is a supported
  escape hatch (see schema.sql "Future work: rich text" — Track 1,
  2026-05-28). Markdown / dual-column approaches are deferred.
- **Categories** strip AGOL's `/Categories/` path prefix before
  comparison, so a just-pushed item doesn't false-flag.

### Single-category invariant

One item → exactly one top-level category. The catalogue is
single-valued, the file lives in one category folder, and
`compute_item_properties` emits a single **absolute** path
(`/Categories/<Parent>[/<Sub>]`). Subcategories are allowed (Species
only at present) as a nested path, not a second top-level membership.
The absolute `/Categories/` prefix is **required** for AGOL to resolve
nested paths — a relative `Species/Other` stores but renders only the
detached leaf (verified live 2026-05-28). Multi-rendition (one dataset
→ multiple AGOL items) is deferred to v2.

### Item categories & folders

- AGOL **folders** mirror the catalogue's underscored category folders
  (`Species`, `Juris_Political_Boundaries`, …), flat namespace.
- AGOL **content categories** mirror the full display names
  (`Jurisdictional & Political Boundaries`). `init-categories` writes
  the 10-category typology (+ Species' subcategories) to the org once.

### Adoption

Items published to AGOL manually before this integration existed carry
an `agol_item_id` but `sync_status='unpublished'`. Migration 009 (and
ad-hoc `y2y agol-sync adopt <id>`) diffs catalogue ↔ AGOL and marks
each `clean` (matched) or `conflict` (drifted — Phase D pull resolves).
Adoption never mutates AGOL.

### Unpublish

`y2y agol-sync unpublish <id>` (confirmation-gated) permanently deletes
the service + linked source, then clears `agol_item_id` /
`agol_published_at` / `last_synced_at` and sets `sync_status=
'unpublished'`. The catalogue row stays active and can be re-pushed.
`permanent=True` frees the service-name reservation immediately so a
later re-push under the same name doesn't collide with the recycle bin.

### Authentication & config

Named-user OAuth, one-time `y2y agol-sync login`, profile cached in
`~/.arcgis/profile_y2y`. `agol_config.load_config()` layers env vars
(`Y2Y_AGOL_CLIENT_ID`, `Y2Y_AGOL_AUTO_PUSH`, `Y2Y_AGOL_PROFILE`) over
an optional `~/.y2y/agol_config.yaml` over defaults. The Conservation
Atlas group ID is resolved on first contact and cached in
`~/.y2y/agol_group_cache.json`. Thumbnails + caches live under `.y2y/`
(git-ignored).

### Why AGOL is downstream, not embedded

AGOL is a publication target, not a storage substrate. Catalogue
mutations never *block* on AGOL — auto-sync is best-effort and failures
are deferred, never fatal. A steward without AGOL credentials can still
run the full local pipeline; only the `y2y agol-sync` commands need a
profile. This keeps the catalogue local-first and the publication step
independently inspectable.

### Scheduling (weekly reconcile)

Not auto-installed — sample configs for the steward to wire up.

**macOS (launchd)** — `~/Library/LaunchAgents/net.y2y.agol-reconcile.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>net.y2y.agol-reconcile</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string><string>-lc</string>
    <string>cd /Users/ethanberman/Dropbox/Earthline/Y2Y/Spatial_Data &amp;&amp; source .venv/bin/activate &amp;&amp; y2y agol-sync reconcile --actor reconcile-cron</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/tmp/y2y-agol-reconcile.log</string>
  <key>StandardErrorPath</key><string>/tmp/y2y-agol-reconcile.err</string>
</dict></plist>
```

Load with `launchctl load ~/Library/LaunchAgents/net.y2y.agol-reconcile.plist`.

**Linux (cron)** — `crontab -e`, Mondays 06:00:

```
0 6 * * 1 cd /path/to/Spatial_Data && ./.venv/bin/y2y agol-sync reconcile --actor reconcile-cron >> /tmp/y2y-agol-reconcile.log 2>&1
```

### Deferred to v2

Multi-rendition (one dataset → both FS and VTL); persistent per-item
sharing override column; async job-monitor for large-raster republish;
multi-user contributor flow; Markdown / dual-column rich text; AGOL-side
audit command for orphan detection.

---

## 16. Scope boundaries (for this and future sessions)

- The pipeline never edits files *inside* a dataset — only moves, ingests,
  and records metadata about them.
- Work-in-progress data does not live in `library/`. It lives elsewhere
  (e.g., working directories outside this repo) and only enters via the
  ingest queue.
- AGOL publishing **is** handled by this library via the `y2y agol-sync`
  sub-group (§15). It remains out-of-band from ingest/lifecycle in the
  sense that catalogue mutations never *block* on AGOL — auto-sync is
  best-effort and AGOL failures are deferred, never fatal.
