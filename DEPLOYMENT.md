# Y2Y Spatial Library — Deployment & Operating Notes

Operator-facing guidance: where the pieces live, and the rules that
keep the catalogue safe. Design rationale is in `DESIGN.md`; command
detail in `COMMAND_REFERENCE.md`.

---

## Code vs. data — two different things

| Thing | Lives in | Distributed via | In git? |
| ----- | -------- | --------------- | ------- |
| **Code** (`pipeline/`, tests, docs) | GitHub | `git pull` / GitHub Release | ✅ yes |
| **Data** (`library/**`, `inventory/inventory.db`, `queue/**`) | one operating machine | not distributed via git | ❌ no (see `.gitignore`) |
| **Catalogue view** (`inventory/inventory.xlsx`) | regenerated from the DB | read-only mirror (SharePoint, etc.) | ❌ no |
| **Published layers** | ArcGIS Online | the Conservation Atlas | n/a |

The data is **deliberately not in git** — geospatial binaries don't
belong there, and the catalogue is a live SQLite database, not a
versionable text file. GitHub carries code; the data is a separate,
single-source-of-truth working copy.

**Access model:**
- A **single steward** adds/edits the catalogue (runs the pipeline).
- Everyone else consumes published layers via **AGOL**.
- The read-only `inventory.xlsx` (and optionally the `library/` files)
  can be **mirrored** to a shared drive for browse/copy access.

---

## The one hard rule: never let a file-sync service touch the *live* DB

`inventory/inventory.db` runs in **WAL mode** — during a write it has
`-wal` and `-shm` sidecar files that must stay consistent *as a set*.
Dropbox / OneDrive / SharePoint sync clients know nothing about SQLite
transactions. If one uploads the `.db` mid-write (or without its matching
`-wal`), the result is a **corrupted catalogue** or a `… (conflicted
copy).db`.

Single-writer reduces this but does **not** eliminate it — a sync firing
during a `y2y` write is enough.

### Operating rules

1. **Pause sync (Dropbox/OneDrive) while running any `y2y` command.**
   Resume only *after* the command fully finishes. This closes the one
   real danger window. Simplest fix, no setup.

2. **One operating machine, period.** Never open/edit the catalogue from
   a second machine that also syncs the same folder. That's the
   conflicted-copy / corruption path that pausing doesn't protect against.

3. **Sync is not backup.** Keep an independent copy of `inventory.db`
   (Time Machine counts, or a periodic
   `cp inventory/inventory.db ~/backups/inventory_$(date +%F).db`).
   A bad ingest or fat-finger should be recoverable independent of sync.

### Set-and-forget alternative (macOS + Dropbox)

Instead of pausing each session, tell Dropbox to ignore the DB files so
it never syncs them:

```bash
cd <project-root>
xattr -w com.dropbox.ignored 1 inventory/inventory.db
xattr -w com.dropbox.ignored 1 inventory/inventory.db-wal 2>/dev/null
xattr -w com.dropbox.ignored 1 inventory/inventory.db-shm 2>/dev/null
```

Ignored = not synced and not backed up by Dropbox — which is fine
architecturally (the `.db` is the steward's source of truth; others read
`inventory.xlsx`, which keeps syncing) — **provided** you have a separate
backup per rule 3.

### Health check

Confirm the catalogue is intact any time:

```bash
source .venv/bin/activate
python -c "import sqlite3; print(sqlite3.connect('inventory/inventory.db').execute('PRAGMA integrity_check').fetchone()[0])"
# expect: ok
```

Also watch for `*conflicted copy*` / `*-conflict*` files appearing in the
tree — their presence means a sync divergence happened and needs manual
resolution before the next run.

---

## Recommended layouts

**A — Working copy local, shared drive is a published mirror (safest).**
The live tree (`library/`, `inventory/`, `queue/`) lives on a local path
*outside* the synced folder; the pipeline runs there with zero sync risk.
After a session, copy `library/` + `inventory.xlsx` to the shared drive
for the read/copy crowd.

**B — Work in the synced folder, pause sync during runs (pragmatic).**
The whole tree sits in the Dropbox/OneDrive/SharePoint folder so files are
"internal" automatically; the steward pauses sync during `y2y` commands
(rules above). This is what most teams actually do; acceptable with a
disciplined single steward and infrequent edits.

Either way: the `library/` files and `inventory.xlsx` are safe to mirror;
the **live `inventory.db` is the only thing that must not be synced
mid-write**.

---

## Handing the steward role to an internal operator

Because the data isn't in git, a handoff needs a **one-time data
transfer** (zip / drive / shared-folder grant) in addition to the code.
Checklist:

1. **Code** — `git clone` the repo; `pip install -e .` into a fresh venv.
2. **Data** — one-time copy of `library/` + `inventory/inventory.db` to
   the operator's machine.
3. **AGOL** — their own `Y2Y_AGOL_CLIENT_ID` + `y2y agol-sync login`.
4. **Docs** — point them at `CHEATSHEET.html` and this file.
5. **Confirm** — `y2y reconcile --deep` (catalogue ↔ files 1:1) and
   `y2y agol-sync status` (AGOL link intact).

After handoff, code flows via GitHub; the data working copy lives with the
operator; the contractor maintains code only.
