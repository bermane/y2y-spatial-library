# Changelog

Releases of the `y2y` pipeline code. The newest release is at the top. Each
release is a git tag (`vX.Y.Z`) on `main`, and `y2y --version` prints the
version that is installed.

## 0.9.0 - 2026-11-24

Pilot release for the first deployment on the data steward's Windows machine.

- Added `y2y --version`.
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
  are laid out in that order, after `ready` and `_validation_error`.
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
- `temporal_start` and `temporal_end` can now be filled, in the review sheet
  and with `y2y update`.
- `agol_item_id` is no longer filled in the review sheet. The first push to
  ArcGIS Online writes it.

## 0.1.0

Development versions before releases were tracked. See the git history.
