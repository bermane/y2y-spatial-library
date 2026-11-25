# Y2Y Spatial Library — Full Command Reference

Exhaustive reference for the `y2y` CLI: every command, every option,
behaviour notes, and lookup tables. For a quick everyday subset see
`CHEATSHEET.md`; for design rationale see `DESIGN.md`; for narrative
how-to see `README.md`.

**Contents**

1. [Environment & invocation](#1-environment--invocation)
2. [Ingest](#2-ingest)
3. [Lifecycle: update / rename / refresh / tombstone](#3-lifecycle)
4. [Reconcile (filesystem)](#4-reconcile-filesystem)
5. [Export](#5-export)
6. [AGOL setup: login / init-categories](#6-agol-setup)
7. [AGOL: status](#7-agol-status)
8. [AGOL: push](#8-agol-push)
9. [AGOL: reconcile (bidirectional)](#9-agol-reconcile-bidirectional)
10. [AGOL: pull](#10-agol-pull)
11. [AGOL: adopt](#11-agol-adopt)
12. [AGOL: unpublish](#12-agol-unpublish)
13. [Vector Tile Layer workflow](#13-vector-tile-layer-workflow)
14. [Reference tables](#14-reference-tables)
15. [Workflows / recipes](#15-workflows--recipes)

---

## 1. Environment & invocation

```bash
cd /path/to/Spatial_Data && source .venv/bin/activate
```

```
y2y [--root DIRECTORY] COMMAND [ARGS]...
```

- `--root DIRECTORY` — project root. Defaults to the current working
  directory. All paths (`queue/`, `library/`, `inventory/`, `reports/`)
  resolve under it.
- `y2y --help` — list all commands.
- `y2y --version` — print the installed version (see `CHANGELOG.md`).
- `y2y <command> --help` — full options for any command.

**Source of truth:** `inventory/inventory.db` (SQLite). `inventory.xlsx`
is a rendered read-only view. AGOL is a downstream publication target;
catalogue mutations never block on it.

**Common option — `--actor NAME`:** every mutating command accepts it to
record who made the change in the changelog. Defaults to `$USER`.

---

## 2. Ingest

```
y2y ingest [--approve] [--actor TEXT]
```

Two phases of one command:

- **Phase 1 (default, no flag) — scan.** Walks `queue/incoming/`,
  inspects each file, and stages accepted source datasets into
  `queue/processing/pending.xlsx` for review. Rejected files go to
  `queue/rejected/` with a `.rejected.yaml` sidecar explaining why.
  `.vtpk` files are dispatched to VTPK-ingest handling (filed into
  `library/vtpk/`, matched to a catalogue row by file stem). A GPKG and
  its same-stem `.vtpk` dropped together are auto-paired.
- **Phase 2 (`--approve`) — promote.** Validates every `ready=TRUE` row
  in `pending.xlsx` (transform → canonical validation → intrinsic
  snapshot), moves the file into `library/spatial/<category>/…`, inserts
  the catalogue row, archives the source bundle to `queue/archived/`,
  and (if logged in) auto-pushes the new row to AGOL.

| Option      | Description |
| ----------- | ----------- |
| `--approve` | Run Phase 2 instead of Phase 1. |
| `--actor`   | Changelog actor at promotion. Defaults to `$USER`. |

**Review-sheet fields** you fill before approve: `title`, `summary`,
`description`, `category`, `subcategory` (Species only), `tags`,
`terms_of_use`, `acknowledgements`, `data_steward`, `agol_format`,
`authoritative`, and `ready` (set to `TRUE`). `agol_format` is pre-filled
from the format and `authoritative` is pre-filled `yes`. Optional:
`temporal_start`, `temporal_end`, `notes`.

**Header colours** (also in the sheet's `legend` tab). Columns are laid out
in this order, with `_validation_error` right after `ready`:

| Colour | Meaning |
| ------ | ------- |
| Yellow | `ready`, the control column. |
| Blue | You fill it; it is published to ArcGIS Online. Lighter blue = optional. |
| Orange | You fill it; it stays in the catalogue. Lighter orange = optional. |
| Grey | Filled by the pipeline. Do not edit. |
| Mauve | Written by the ArcGIS Online sync (`agol_item_id`). Do not edit. |
| Red | `_validation_error`, the pipeline's error message. |

**Field rules**

- `tags`: separate tags with `;` (a `,` also works). The cell cannot be
  empty. Enter `NA` (any letter case) on its own if the dataset should
  have no tags; `NA` is never published as a tag and cannot be mixed
  with real tags.
- `authoritative`: `yes` or `no`. `yes` gives the ArcGIS Online item the
  "Authoritative" badge at push; `no` removes it.
- `temporal_start` / `temporal_end`: the period the data covers, as
  `YYYY`, `YYYY-MM` or `YYYY-MM-DD`. Leave empty if it does not apply.
- `agol_item_id`: leave empty. It is filled in when the dataset is
  published.

**On validation failure:** the row's `ready` flips back to `FALSE` and
`_validation_error` explains the problem. Fix the underlying issue, set
`ready=TRUE`, re-run `y2y ingest --approve`.

**Typical flow**
```bash
# drop files into queue/incoming/
y2y ingest
open queue/processing/pending.xlsx     # edit + set ready=TRUE
y2y ingest --approve
```

---

## 3. Lifecycle

### `y2y update`

```
y2y update [--set KEY=VALUE]... [--actor TEXT] [--yes] DATASET_ID
```

Update non-locked metadata fields. Repeat `--set` per field.

| Option    | Description |
| --------- | ----------- |
| `--set`   | Field to update, as `KEY=VALUE`. Repeatable. |
| `--actor` | Changelog actor. Defaults to `$USER`. |
| `--yes`   | Skip the confirmation asked before deprecating a dataset that is published on AGOL. |

**Updatable fields:** `title`, `status` (`active` / `deprecated`),
`classification` (raster only), `data_steward`, `summary`,
`description`, `tags` (`;`-delimited, or `NA` for none), `terms_of_use`,
`acknowledgements`, `agol_item_id`, `internal_notes`, `agol_format`,
`authoritative` (`yes` / `no`), `temporal_start`, `temporal_end`
(`YYYY`, `YYYY-MM` or `YYYY-MM-DD`; empty clears). `agol_item_id` is
normally filled by the push; set it by hand only before
`y2y agol-sync adopt`.

Rejected: locked columns (`checksum_sha256`, `size_bytes`, `mtime`,
`library_crs`, `geographic_extent_bbox` — only `refresh` changes these) and
movement-bound columns (`file_path`, `category`, `subcategory` — use
`rename`). `status='tombstoned'` is rejected here — use `tombstone`.

**Deprecating removes the layer from AGOL.** `--set status=deprecated`
on a published dataset warns you, asks for confirmation, then
permanently deletes its AGOL item(s). The library file and the
catalogue row are kept. Answering no changes nothing. If AGOL cannot be
reached, the status still changes and the next
`y2y agol-sync reconcile` removes the layer. Web maps and apps that use
the layer will show it as broken, and styling saved on the layer is
lost, so check on AGOL first when in doubt. `--set status=active`
publishes the dataset again as a **new** item with a new ID.

```bash
y2y update ds_01ABC --set summary="Revised text"
y2y update ds_01ABC --set tags="caribou;telemetry" --set data_steward="Ethan"
y2y update ds_01ABC --set agol_format=vector-tile-layer
y2y update ds_01ABC --set tags=NA --set authoritative=no
y2y update ds_01ABC --set temporal_start=2020 --set temporal_end=2020
y2y update ds_01ABC --set status=deprecated     # also removes it from AGOL
y2y update ds_01ABC --set status=active         # publishes it again (new item)
```

### `y2y rename`

```
y2y rename [--actor TEXT] DATASET_ID NEW_PATH
```

Move a file within `library/` and update `file_path` (+ derived
`category` / `subcategory`). `NEW_PATH` is library-relative using
folder-name conventions and must pass the naming validator.

Handles three filesystem situations: file at old path only (moves it);
file already at new path (records the rename you did manually); file at
both or neither (errors — resolve manually).

```bash
y2y rename ds_01ABC "Water/streams_v2.gpkg"
y2y rename ds_01ABC "Species/Caribou/dens_2024.gpkg"
```

### `y2y refresh`

```
y2y refresh [--actor TEXT] DATASET_ID
```

Re-stat the canonical file after editing it in place (added a field,
recomputed values, rebuilt overviews). Recomputes checksum / size /
mtime / crs / bbox from disk. Refuses if the file no longer passes
canonical validators. No-op when nothing changed.

```bash
y2y refresh ds_01ABC
```

### `y2y tombstone`

```
y2y tombstone [--reason TEXT] [--actor TEXT] [--yes] DATASET_ID
```

Soft-delete: set `status='tombstoned'` and delete the file from
`library/`. The row stays permanently as an audit record;
`dataset_id` stays reserved. Reconcile thereafter expects the file
absent. Confirmation-prompted unless `--yes`. If the dataset is
published on AGOL, the prompt says so and its AGOL item(s) are
permanently deleted as well; if AGOL cannot be reached, the next
`y2y agol-sync reconcile` removes them.

| Option     | Description |
| ---------- | ----------- |
| `--reason` | Optional reason recorded in the changelog. |
| `--actor`  | Changelog actor. Defaults to `$USER`. |
| `--yes`    | Skip the confirmation prompt. |

```bash
y2y tombstone ds_01ABC --reason "superseded by 2025 update"
```

**Auto-sync note:** `update` / `rename` / `refresh` on a published row
auto-mark it `pending_push` and attempt a best-effort AGOL push (see §8).
Deprecating or tombstoning a published row removes its AGOL item(s)
instead.

---

## 4. Reconcile (filesystem)

```
y2y reconcile [--deep] [--fix-renames] [--actor TEXT]
```

Detect drift between `library/` and the catalogue; write a timestamped
markdown report to `reports/reconcile_<ts>_<mode>.md`.

| Option          | Description |
| --------------- | ----------- |
| `--deep`        | Recompute SHA-256 for every file (vs stat-only: size + mtime). |
| `--fix-renames` | Prompt to confirm + apply each detected rename. Implies `--deep`. |
| `--actor`       | Changelog actor when applying fixes. Defaults to `$USER`. |

**Findings:** orphans (file, no row), ghosts (row, no file), drift
(content changed + non-canonical), schema violations, renames
(deep-mode ghost+orphan checksum pairs), auto-resolved drift (content
changed but still canonical — snapshot auto-refreshed), and VTPK
invariants (missing / stale / orphan).

**Mutations:** drift on still-canonical files auto-refreshes the
snapshot (changelog `refresh`); renames apply only with
`--fix-renames`. Orphans, ghosts, and schema violations surface for
manual handling — never auto-fixed.

```bash
y2y reconcile
y2y reconcile --deep
y2y reconcile --fix-renames
```

---

## 5. Export

```
y2y export-xlsx [--out FILE]
```

Render the catalogue as a steward-friendly read-only `inventory.xlsx`.
Editing the xlsx changes nothing — re-export overwrites it. Most
mutating commands auto-export, so manual use is rare.

| Option  | Description |
| ------- | ----------- |
| `--out` | Output path. Defaults to `inventory/inventory.xlsx` under `--root`. |

```bash
y2y export-xlsx
y2y export-xlsx --out /tmp/inventory_snapshot.xlsx
```

---

## 6. AGOL setup

### `y2y agol-sync login`

```
y2y agol-sync login
```

Interactive OAuth. Requires the OAuth client id in
`Y2Y_AGOL_CLIENT_ID` (or `~/.y2y/agol_config.yaml`). Opens a browser
for consent once; caches credentials at `~/.arcgis/profile_y2y`.

```bash
export Y2Y_AGOL_CLIENT_ID="<oauth-client-id>"
y2y agol-sync login
```

### `y2y agol-sync init-categories`

```
y2y agol-sync init-categories [--dry-run] [--yes]
```

Write the canonical AGOL org category schema from `pipeline/taxonomy.py`
(10 top-level categories + 7 Species subcategories). **Destructive** —
categories not in the canonical typology are orphaned, and items tagged
with them lose those tags. Requires org-admin.

| Option      | Description |
| ----------- | ----------- |
| `--dry-run` | Show the diff without writing to AGOL. |
| `--yes`     | Skip the confirmation prompt (for scripting). |

```bash
y2y agol-sync init-categories --dry-run
y2y agol-sync init-categories
```

---

## 7. AGOL: status

```
y2y agol-sync status [--deep]
```

Report `sync_status` distribution across every `active` row.

| Option   | Description |
| -------- | ----------- |
| `--deep` | Also query AGOL for each linked item's modified timestamp and flag rows where AGOL has drifted past `last_synced_at`. |

```bash
y2y agol-sync status
y2y agol-sync status --deep
```

---

## 8. AGOL: push

```
y2y agol-sync push [OPTIONS] [DATASET_ID]
```

Publish or update a dataset (or a batch). The publish target comes from
the row's `agol_format` unless `--target` overrides it. Sharing defaults
to org + Y2Y Conservation Atlas group.

| Option              | Description |
| ------------------- | ----------- |
| `--all-dirty`       | Push every row with `sync_status='pending_push'`. No `DATASET_ID`. |
| `--all-unpublished` | Push every row with `sync_status='unpublished'` — the initial backlog publish. No `DATASET_ID`. |
| `--target`          | `feature-layer` / `vector-tile-layer` / `imagery-layer`. Override the row's `agol_format` for this invocation only (ad-hoc). Not allowed with batch flags. For a durable change use `y2y update <id> --set agol_format=…`. |
| `--sharing`         | `private` (owner only) / `org` (org-visible, no group) / `public` (world, no group). Overrides the default. |
| `--dry-run`         | Show the plan without contacting AGOL or mutating the catalogue. |
| `--actor`           | Changelog actor. Defaults to `$USER`. |

**Rules:** `--all-dirty` and `--all-unpublished` are mutually exclusive
and can't take a `DATASET_ID`. Batch per-row failures mark that row
`error` and the batch continues. VTL rows lacking a VTPK fail cleanly
and are reported.

**Per-row footprint on AGOL:** vectors and VTL produce two items — a
private *source* in `_sources/` and the public *service* in the category
folder (the `agol_item_id`). Imagery uses a no-source model (one item).

**Data export:** the first time a feature layer is published, push turns
on "Allow others to export to different formats", so anyone in the
organization can download the data from the layer's item page. Later
pushes leave that setting as it is on AGOL, so you can turn it off by
hand for a dataset that must not be downloaded. Vector tile and imagery
layers have no export setting and stay view-only.

```bash
y2y agol-sync push ds_01ABC                       # one row
y2y agol-sync push ds_01ABC --dry-run             # preview
y2y agol-sync push ds_01ABC --target vector-tile-layer
y2y agol-sync push ds_01ABC --sharing private
y2y agol-sync push --all-unpublished --dry-run    # preview backlog
y2y agol-sync push --all-unpublished              # publish backlog
y2y agol-sync push --all-dirty                    # push edited rows
```

**Auto-sync:** with `Y2Y_AGOL_AUTO_PUSH=true` (default), catalogue edits
and ingest approvals attempt an immediate best-effort push when you're
logged in. Failures are deferred (changelog-logged, never fatal); the
row waits for the next `reconcile` / `push --all-dirty`. Set
`Y2Y_AGOL_AUTO_PUSH=false` to make all pushes manual.

---

## 9. AGOL: reconcile (bidirectional)

```
y2y agol-sync reconcile [--dry-run] [--actor TEXT]
```

First, remove the AGOL item(s) of every `deprecated` or `tombstoned`
row that is still linked to AGOL (the safety net for a removal that
could not happen when the status changed). Then, for every `active`
row: push `pending_push` (failures → `error`); check
`clean` rows for AGOL-side drift (item `modified` > `last_synced_at`) and
flag drifted ones `pending_pull`; retry `error` rows once; skip
`unpublished` / `pending_pull` / `conflict`. Writes
`reports/agol_reconcile_<ts>.md`. Intended for a weekly schedule.

**Group audit.** Each run (including `--dry-run`) also compares the
Y2Y Conservation Atlas group with the catalogue and adds a "Group
audit" section to the report. It is read-only: nothing is changed in
the catalogue or on AGOL. It lists:

- catalogue datasets on AGOL that are not shared to the group;
- catalogue datasets whose AGOL item no longer exists;
- layers in the group that the catalogue does not track, split into
  those hosted by Y2Y and those from outside providers;
- layers published by the pipeline that no catalogue dataset links to;
- layers of deprecated or tombstoned datasets;
- a count of items that are not layers (web maps, apps, dashboards).

If the group can't be read, the report says the audit did not run and
the rest of the reconcile is unaffected.

| Option      | Description |
| ----------- | ----------- |
| `--dry-run` | Compute outcomes + write the report without mutating, pushing or deleting. Lists the layers that would be removed. |
| `--actor`   | Changelog actor. Defaults to `$USER` (use `reconcile-cron` for scheduled runs). |

```bash
y2y agol-sync reconcile --dry-run
y2y agol-sync reconcile
y2y agol-sync reconcile --actor reconcile-cron
```

Scheduling samples (launchd / cron) are in `DESIGN.md §15`.

---

## 10. AGOL: pull

```
y2y agol-sync pull [OPTIONS] [DATASET_ID]
```

Resolve catalogue ↔ AGOL drift. Three single-row modes plus a batch
surface mode.

| Option          | Description |
| --------------- | ----------- |
| (no flag)       | Fetch + diff + mark `sync_status='conflict'`, log the per-field diff. No data changes. |
| `--accept`      | Catalogue absorbs AGOL's drifted text fields (title, summary, description, tags, acknowledgements, terms_of_use). Marks `clean`. |
| `--reject`      | Re-push catalogue values to AGOL, overwriting the drift. (Flips to `pending_push`, then pushes.) |
| `--all-pending` | Surface diffs for every `pending_pull` row (no auto-resolution). Mutually exclusive with `DATASET_ID` / `--accept` / `--reject`. |
| `--actor`       | Changelog actor. Defaults to `$USER`. |

`--accept` skips the `categories` field — it's filesystem-bound; change
a dataset's category with `y2y rename` (a file move), not a pull.

```bash
y2y agol-sync pull ds_01ABC               # surface the diff
y2y agol-sync pull ds_01ABC --accept      # take AGOL's text edits
y2y agol-sync pull ds_01ABC --reject      # re-assert the catalogue
y2y agol-sync pull --all-pending          # review all drifted rows
```

---

## 11. AGOL: adopt

```
y2y agol-sync adopt [--actor TEXT] DATASET_ID
```

Bring a manually-published item under sync management: for a row whose
`agol_item_id` is set but `sync_status='unpublished'`, fetch the AGOL
item, diff it field-by-field, and mark the row `clean` (matched) or
`conflict` (drifted — resolve via `pull`). Never mutates AGOL. (Migration
009 ran this across all candidates at once; this is the ad-hoc form.)

| Option    | Description |
| --------- | ----------- |
| `--actor` | Changelog actor. Defaults to `$USER`. |

```bash
y2y agol-sync adopt ds_01ABC
```

---

## 12. AGOL: unpublish

```
y2y agol-sync unpublish [--actor TEXT] [--yes] DATASET_ID
```

Permanently delete the AGOL service + any linked source item, then clear
`agol_item_id` / `agol_published_at` / `last_synced_at` and set
`sync_status='unpublished'`. The catalogue row keeps its status; an
`active` row can be re-pushed. Confirmation-prompted unless `--yes`.

Use this to take an **active** dataset off AGOL. Deprecating or
tombstoning a dataset does the same removal automatically.

If AGOL refuses to delete the layer (delete protection, a dependent
hosted view, a network error), the command fails, the link is **kept**
and the row is set to `sync_status='error'`, because the layer is still
live. Fix the cause on AGOL and run it again. A failure to delete only
the linked source item is recorded as an `[agol]` warning (in
`internal_notes` + the changelog) and does not block the unlink.

| Option    | Description |
| --------- | ----------- |
| `--actor` | Changelog actor. Defaults to `$USER`. |
| `--yes`   | Skip the confirmation prompt. |

```bash
y2y agol-sync unpublish ds_01ABC
y2y agol-sync unpublish ds_01ABC --yes
```

---

## 13. Vector Tile Layer workflow

The pipeline never imports arcpy — the steward builds the VTPK once per
source-data change in ArcGIS Pro.

```bash
# 1. Set the target on the row:
y2y update ds_01ABC --set agol_format=vector-tile-layer

# 2. In ArcGIS Pro: open the GPKG → Share As → Vector Tile Package →
#    save as <file_stem>.vtpk (matching the catalogue file's stem).

# 3. Drop the .vtpk in queue/incoming/, then file it into library/vtpk/:
y2y ingest          # scan recognises the .vtpk extension

# 4. Publish:
y2y agol-sync push ds_01ABC
```

A GPKG and its same-stem `.vtpk` dropped together in `queue/incoming/`
are auto-paired during scan and both land on approve. `y2y reconcile`
flags VTL rows whose VTPK is **missing** or **stale** (older than the
source GPKG), and `.vtpk` files on disk with no matching VTL row
(**orphan**). Refreshing a VTL: rebuild the VTPK in Pro, re-ingest, push
— the source is re-published and the VTS item id stays stable.

---

## 14. Reference tables

### `sync_status` values

| Value          | Meaning |
| -------------- | ------- |
| `unpublished`  | Never pushed (or torn down by `unpublish`). |
| `clean`        | Catalogue ↔ AGOL in sync. |
| `pending_push` | Catalogue edited since last sync; owes a push. |
| `pending_pull` | AGOL modified since last sync; needs a `pull`. |
| `conflict`     | Diff surfaced, awaiting steward resolution. |
| `error`        | Last push failed; `reconcile` retries once. |

### State transitions

```
unpublished ──push────────────────> clean
unpublished ──adopt(match)─────────> clean
unpublished ──adopt(drift)─────────> conflict
clean ──catalogue edit─────────────> pending_push
clean ──reconcile finds AGOL drift─> pending_pull
pending_push ──push ok─────────────> clean
pending_push ──push fails──────────> error
pending_pull ──pull --accept|reject> clean
pending_pull ──pull (no flag)──────> conflict
conflict ──pull --accept───────────> clean
conflict ──pull --reject───────────> clean (via pending_push)
error ──reconcile retry ok─────────> clean
any-with-item ──unpublish──────────> unpublished
any-with-item ──unpublish refused──> error (link kept)
any-with-item ──deprecate/tombstone─> unpublished (layer removed)
```

### `agol_format` values

| Value               | Source format | AGOL result |
| ------------------- | ------------- | ----------- |
| `feature-layer`     | geopackage    | Hosted Feature Layer (default for GPKG). |
| `imagery-layer`     | geotiff       | Hosted Imagery Layer (default for GeoTIFF; no-source model). |
| `vector-tile-layer` | geopackage    | Vector Tile Layer — needs a steward-built VTPK (§13). |

### Environment variables

| Variable              | Default | Purpose |
| --------------------- | ------- | ------- |
| `Y2Y_AGOL_CLIENT_ID`  | —       | OAuth client id; required for `login`. Keep out of git. |
| `Y2Y_AGOL_AUTO_PUSH`  | `true`  | Auto-push on catalogue edits / ingest approvals. `false` = all pushes manual. |
| `Y2Y_AGOL_PROFILE`    | `y2y`   | Override the cached OAuth profile name. |

Optional config file: `~/.y2y/agol_config.yaml` (portal URL, profile
name, group name, client id). Resolved group id cached in
`~/.y2y/agol_group_cache.json`.

### Directory layout

| Path                       | Role |
| -------------------------- | ---- |
| `queue/incoming/`          | Drop new source files (+ `.vtpk`) here. |
| `queue/processing/`        | `pending.xlsx` review sheet during ingest. |
| `queue/rejected/`          | Rejected files + `.rejected.yaml` sidecars. |
| `queue/archived/`          | Source bundles, post-approval. |
| `library/spatial/`         | Canonical typed datasets (source-of-truth files). |
| `library/vtpk/`            | Ingested VTPKs for VTL rows. |
| `inventory/inventory.db`   | **The catalogue — source of truth.** |
| `inventory/inventory.xlsx` | Rendered read-only view. |
| `reports/`                 | Reconcile reports (filesystem + AGOL). |
| `.y2y/`                    | Thumbnail + group-id caches (git-ignored). |

### Sharing levels (`--sharing`)

| Value     | Visibility |
| --------- | ---------- |
| (default) | Org + Y2Y Conservation Atlas group. |
| `private` | Owner only. |
| `org`     | Org-visible, no group. |
| `public`  | World-visible, no group. |

---

## 15. Workflows / recipes

### First-time AGOL setup
```bash
export Y2Y_AGOL_CLIENT_ID="<oauth-client-id>"
y2y agol-sync login
y2y agol-sync init-categories --dry-run
y2y agol-sync init-categories
```

### Ingest a batch end-to-end
```bash
# drop files into queue/incoming/
y2y ingest
open queue/processing/pending.xlsx        # fill fields, set ready=TRUE
y2y ingest --approve
y2y reconcile --deep                       # confirm catalogue ↔ filesystem 1:1
```

### Initial bulk publish of the backlog
```bash
y2y agol-sync push --all-unpublished --dry-run    # review
y2y agol-sync push --all-unpublished              # publish
y2y agol-sync reconcile                            # retry any errors, get report
```

### Controlled first publish (no auto-sync noise during ingest)
```bash
export Y2Y_AGOL_AUTO_PUSH=false
# ... ingest the whole backlog ...
unset Y2Y_AGOL_AUTO_PUSH
y2y agol-sync push --all-unpublished
```

### Edit metadata and let it propagate
```bash
y2y update ds_01ABC --set summary="Revised" --set tags="a;b;c"
# auto-pushes if logged in; otherwise next reconcile picks it up
```

### Replace a dataset with a new edition
```bash
# ingest the new edition as its own dataset, then retire the old one
y2y update ds_01OLD --set status=deprecated
# confirms, then removes the old layer from AGOL; file + row are kept
```

### Resolve AGOL-side edits
```bash
y2y agol-sync reconcile           # flags drifted rows pending_pull
y2y agol-sync pull --all-pending  # review each diff
y2y agol-sync pull ds_01ABC --accept   # or --reject, per row
```

### Full health check
```bash
y2y reconcile --deep              # filesystem integrity
y2y agol-sync status --deep       # AGOL sync state + drift
pytest tests/                      # test suite (dev only)
```
