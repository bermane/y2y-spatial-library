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
| *Does this dataset exist? Where is it?*    | The filesystem (`library/`) |
| *What is its title, CRS, license, version, steward, AGOL linkage, history?* | The inventory (`inventory/inventory.xlsx`) |

**Why:** Spatial datasets are first-class filesystem objects — GIS tools
open them from disk, they get copied, renamed, moved. A database as the sole
source of truth would constantly diverge from what's actually on disk.
Conversely, the filesystem can't capture provenance, licensing, or AGOL
linkage. Splitting the concern keeps each store doing what it's naturally
good at.

**Consequence:** Drift between the two is *expected* (the steward edits
files in the normal course of work) and must be *detectable* (see §2).

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

`inventory/changelog.md` is written only by
`pipeline.inventory_manager.append_changelog()`. It is never regenerated,
never edited, never sorted. Past entries are immutable.

**Why:** It's the audit trail. A changelog that can be rewritten is not an
audit trail — it's a note file. Preserving append-only semantics means that
six months from now, the steward (or an auditor, or a funder) can
reconstruct exactly what happened and when. Corrections are themselves
logged, not applied in-place.

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

One row per dataset in `inventory/inventory.xlsx`.

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
The on-disk **column order** in `inventory.xlsx` does not match these
groupings — it's tuned for the steward's eye-flow when reading the
sheet (identity → AGOL content → state → location → snapshot →
freeform → source provenance at the back). The authoritative on-disk
order is in `pipeline/inventory_manager.py:INVENTORY_COLUMNS`.

#### Identity & location (required)

| Field         | Type              | Notes                                                                 |
| ------------- | ----------------- | --------------------------------------------------------------------- |
| `dataset_id`  | string (opaque)   | Stable across renames/moves. Primary key. Assigned at scan.           |
| `category`    | enum              | One of the 9 taxonomy categories — stored as the **display name** (full name from the typology file, e.g. `Administrative & Jurisdictional Boundaries`). The on-disk folder uses the underscored abbreviation (`Admin_Juris_Boundaries`); the pipeline maps display↔folder. |
| `subcategory` | string (nullable) | Display sub-name (e.g. `Grizzly Bear`); folder is `Grizzly_Bear`. Only `Species & Species at Risk` has subcategories in Phase A. |
| `file_path`   | string (relative) | Path of the **canonical** file relative to `library/`, using folder names. Set at approve. |
| `format`      | enum              | Hard enum: `GeoPackage` or `Cloud Optimized GeoTIFF`. The output of transformation; admitting another canonical format would be a deliberate schema change. |

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
| `source_format`   | enum              | `Shapefile`, `GeoPackage`, `GeoJSON`, `KML`, or `GeoTIFF` (Phase A allow-list). |
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
| `mtime`                  | ISO-8601 datetime | Modification time at last known-good state.                            |
| `geographic_extent_bbox` | string            | `minx,miny,maxx,maxy` in the canonical CRS. Snapshot only.             |

#### Classification (raster-only, overridable)

| Field            | Type | Notes                                                                                                     |
| ---------------- | ---- | --------------------------------------------------------------------------------------------------------- |
| `classification` | enum | `continuous` or `categorical` for raster datasets; null for vector. Steward-declared in the review sheet. Drives reprojection resampling (bilinear vs nearest), TIFF predictor (3 vs 2), and default NoData. See §11. |

#### History & governance (required)

| Field           | Type              | Notes                                                                 |
| --------------- | ----------------- | --------------------------------------------------------------------- |
| `status`        | enum              | `active`, `deprecated`, or `tombstoned`. Defaults to `active` at ingestion. See below for semantics. |
| `date_added`    | ISO-8601 date     | Ingestion date.                                                       |
| `date_modified` | ISO-8601 date     | Last in-place modification recorded by the pipeline.                  |
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

| Field          | Type              | Required | Notes                                                                |
| -------------- | ----------------- | -------- | -------------------------------------------------------------------- |
| `agol_item_id` | string            | no       | AGOL item ID once published. Null between ingestion and publication. |
| `notes`        | string            | no       | Free-form. Don't encode structured data here — add a column instead. |

### Open questions

None currently open — prior items resolved: `status` column added
(soft-delete via `tombstoned`), checksums cover the whole bundle for
multi-file formats, extrinsic metadata is required for every dataset,
descriptions are plain text, `terms_of_use` and `acknowledgements` stay
separate with duplication accepted.

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
   `library/<Category>/[<Subcategory>/]<target_filename>`. Append the
   finalised row to `inventory.xlsx` (with `ready`,
   `target_filename`, `_validation_error`
   stripped). Append an `add` entry to `changelog.md` that records
   both the source format and target path.
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

Four operations on rows that are already in `inventory.xlsx`. Each
appends a corresponding action to the changelog.

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
  `notes`, `classification`, plus `status` transitions between
  `active` and `deprecated`.
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

## 12. Scope boundaries (for this and future sessions)

- The pipeline never edits files *inside* a dataset — only moves, ingests,
  and records metadata about them.
- Work-in-progress data does not live in `library/`. It lives elsewhere
  (e.g., working directories outside this repo) and only enters via the
  ingest queue.
- AGOL publishing is *not* handled by this library. The inventory records
  the AGOL `item_id` when known, but pushing or syncing to AGOL is a
  separate concern.
