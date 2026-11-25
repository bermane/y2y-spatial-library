# Changelog

Releases of the `y2y` pipeline code. The newest release is at the top. Each
release is a git tag (`vX.Y.Z`) on `main`, and `y2y --version` prints the
version that is installed.

## 0.9.0 - 2026-11-24

Pilot release for the first deployment on the data steward's Windows machine.

- Added `y2y --version`.
- Newly published feature layers now let organization members export the data
  ("Allow others to export to different formats"). Turning it off by hand on
  ArcGIS Online is respected by later pushes.
- Newly published layers now get a standard default style: polygons as an
  outline with no fill, lines and points in the colour of the dataset's
  category, continuous rasters on a viridis ramp and categorical rasters in
  colour-blind-friendly class colours, both 30% transparent. The style is set
  once, on first publish. Later pushes and reconcile never touch it, so a
  style changed by hand on ArcGIS Online is kept. The colours and defaults are
  a first version, to be checked and reviewed with the data steward for 0.9.1.
- Styling that comes with the data is kept. Ingest now copies a raster's
  colour table into the library file, and publishing uses it. A vector tile
  package styled by attribute in ArcGIS Pro is published as built.
- New `y2y agol-sync restyle <id>` resets one published layer to the standard
  default style, after a confirmation. It saves the style it replaces to
  `reports/restyle_<dataset_id>_<timestamp>.json` first.
- Fixed: on Windows, ingest stored a dataset's `file_path` with backslashes, so
  `y2y reconcile` reported every dataset as missing or orphaned. Paths are now
  stored with forward slashes on every system, and `y2y rename` accepts a path
  typed with backslashes.
- Fixed: rasters are now published as tiled imagery layers. The dynamic
  imagery layers the pipeline made before did not open in Map Viewer.
- Fixed: retiring a dataset marked authoritative failed, because ArcGIS
  Online puts delete protection on authoritative layers. Removal now turns
  the protection off first, and deletes nothing if that fails.
- The version is now set in one place, `pipeline/__init__.py`.
- Added `six` to the dependencies. A fresh install was missing it, which broke
  publishing rasters as imagery layers.
- Added `CLAUDE.md` and this changelog.
- Windows setup now targets Python 3.13 (3.12 also works) and refuses 3.14 and
  newer, where the install fails because fiona has no build yet.
- Windows installs and updates now use the tested package versions pinned in
  `constraints-windows.txt`.
- `setup_windows.ps1` now runs the full test suite and reports the result.
- Replaced a deprecated geopandas call in the tests. geopandas 1.0 or newer is
  now required.
- New header colours in `pending.xlsx` and `inventory.xlsx`, with a `legend`
  tab in each. Blue columns are filled by the steward and published to ArcGIS
  Online, orange columns are filled by the steward and stay in the catalogue,
  grey columns are filled by the pipeline, and mauve columns are written by
  the ArcGIS Online sync. A lighter shade marks an optional column. Columns
  are laid out in that order, after `ready` and `_validation_error`. The grey
  block runs dates, source columns, library columns, then checksum and sizes.
- `tags` can now say "no tags": enter `NA` (any letter case) on its own. The
  cell still cannot be empty, `NA` cannot be mixed with real tags, and it is
  never published as a tag. Tags may be separated with `;` or `,`, and
  duplicates are dropped.
- New `authoritative` column (`yes` / `no`, pre-filled `yes`), in the review
  sheet, the inventory and `y2y update`. An existing catalogue needs
  migration 011; a new one already has the column.
- Pushing to ArcGIS Online now sets the item's "Authoritative" badge from that
  column, and removes it when the column says `no`. This needs an account
  that is an administrator or has the privilege to mark content as
  authoritative. Without it the push still succeeds and a warning is written
  to `internal_notes` and the changelog.
- `title` is pre-filled in the review sheet with the source file name, minus
  its extension, as a starting point to edit.
- `temporal_start` and `temporal_end` can now be filled, in the review sheet
  and with `y2y update`.
- Renamed the catalogue columns `format` and `crs` to `library_format` and
  `library_crs`, so they read clearly beside `source_format` and
  `source_crs`. An existing catalogue needs migration 012; a new one already
  has the new names.
- `agol_item_id` is no longer filled in the review sheet. The first push to
  ArcGIS Online writes it.
- `y2y agol-sync reconcile` now audits the Y2Y Conservation Atlas group. The
  weekly report lists layers in the group that the catalogue does not track,
  and catalogue datasets on ArcGIS Online that are not shared to the group.
  The audit only reports; it changes nothing.
- A push that cannot share its layer to the group now records a warning
  instead of passing silently.
- Setting a dataset to `deprecated` now removes its layer from ArcGIS Online.
  `y2y update <id> --set status=deprecated` warns, asks for confirmation
  (`--yes` skips it), then deletes the layer and its source item. The library
  file and the catalogue row are kept. Setting the status back to `active`
  publishes the dataset again as a new item.
- `y2y tombstone` now removes the dataset's ArcGIS Online layer as well.
  Running `y2y agol-sync unpublish` first is no longer needed.
- `y2y agol-sync reconcile` removes the layer of any deprecated or tombstoned
  dataset that is still on ArcGIS Online, so a removal that could not happen
  at the time (offline, expired sign-in) completes on the next run.
  `--dry-run` lists what would be removed, and `y2y agol-sync status` lists
  the datasets that are waiting.
- If ArcGIS Online refuses to delete a layer, `unpublish` now keeps the
  catalogue link and marks the row `error`, instead of forgetting a layer
  that is still live.
- A deprecated dataset is no longer left stuck in `pending_push`.

## 0.1.0

Development versions before releases were tracked. See the git history.
