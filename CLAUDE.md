# CLAUDE.md

Python CLI (`y2y`, package `pipeline/`) for the Y2Y spatial data library. See DESIGN.md
for rationale, DEPLOYMENT.md for operating rules, ONBOARDING_WINDOWS.md for the
operator setup. README.md is partly stale; prefer code and DESIGN.md.

## Ground rules

- Get the maintainer's OK before editing code or docs, committing, or pushing.
- The operator runs Windows. Keep everything cross-platform, and keep `scripts/*.ps1`
  pure ASCII.
- This repo holds code only. `library/**`, `inventory/*.db`, and `queue/**` are data,
  are gitignored, and live on the operator's machine under `Y2Y_ROOT`.
- Never run `y2y agol-sync` against live ArcGIS Online from a development machine or
  an example data root. Only the operator's machine syncs with AGOL.
- Auto-push stays on (the default) for the operator. Development machines set
  `Y2Y_AGOL_AUTO_PUSH=false`, because `update`, `rename`, `refresh` and
  `ingest --approve` otherwise attempt a push.

## Commands

```
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                          # full suite
pytest tests/test_x.py -k name  # one test
ruff check .                    # line length 100, target py311
y2y --help                      # or: python -m pipeline
```

## Layout

- `pipeline/__main__.py`: Click CLI. `--root` / `Y2Y_ROOT` selects the data root.
- `ingest.py`, `pending_sheet.py`, `source_formats.py`, `transformations.py`,
  `validators/`: three-phase ingest (scan, Excel review sheet, approve).
- `lifecycle.py`: update, rename, refresh, tombstone. `reconcile.py`: drift detection.
- `db.py`, `schema.sql`, `inventory_manager.py`, `export_xlsx.py`: SQLite catalogue
  and its xlsx view. `migrations/NNN_*.py`: numbered, one-way.
- `agol_*.py`: ArcGIS Online sync (push, pull, reconcile, adopt, unpublish, VTPK).
- `taxonomy.py`: the 10 categories and Species subcategories.

## Invariants

- SQLite is the source of truth. Open it only through `db.get_connection()`.
  Never write to `inventory.xlsx` as data; it is regenerated.
- Canonical output is GPKG or COG in ESRI:102008, filed under
  `library/spatial/<Category>/[<Subcategory>/]`. Validators run on the output at
  approve, not on sources at scan.
- The pipeline never edits a dataset's contents, and reconcile does not auto-fix
  beyond canonical drift refresh and confirmed renames.
- Rows are never deleted. Tombstone keeps the row. The changelog is append-only.
- AGOL is downstream. An AGOL failure must never block a catalogue write.
- No arcpy. VTPKs are built by hand in ArcGIS Pro and ingested.
- The category list is duplicated in `taxonomy.py` and the `schema.sql` CHECK
  constraint. Change both, plus a migration, together.
- Schema changes follow the PostGIS-portability rules in DESIGN.md §13.
- Never commit secrets. `Y2Y_AGOL_CLIENT_ID` comes from the environment.
- Tests must not touch a real data root or call AGOL; use the fixtures in
  `tests/conftest.py`.
- When CLI behaviour changes, update CHEATSHEET and COMMAND_REFERENCE (.md and .html).
- The version lives only in `pipeline/__init__.py`. Every release bumps it, adds a
  `CHANGELOG.md` entry, and is tagged `vX.Y.Z` on `main` (see DEPLOYMENT.md).
