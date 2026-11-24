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

## 0.1.0

Development versions before releases were tracked. See the git history.
