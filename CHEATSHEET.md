# Y2Y Spatial Library — One-Page Cheat Sheet

Everyday commands. Full detail in `COMMAND_REFERENCE.md`. Design in `DESIGN.md`.

```powershell
# START OF SESSION (run once per new PowerShell window)
C:\Y2Y\y2y-spatial-library\.venv\Scripts\Activate.ps1
# then pause OneDrive sync: system tray -> OneDrive -> gear -> Pause syncing
```

`inventory/inventory.db` is the source of truth. The xlsx is a read-only view. AGOL is downstream.
Commands are shown for Windows PowerShell. The data folder is the one `Y2Y_ROOT` points to.
`<id>` is a dataset's `dataset_id` (`ds_01…`): look it up in `inventory\inventory.xlsx`.

---

### Ingest new data  ·  scan → review → approve
```powershell
y2y ingest                                            # scan queue\incoming\ → pending.xlsx
start "$env:Y2Y_ROOT\queue\processing\pending.xlsx"  # fill fields, set ready=TRUE, close Excel
y2y ingest --approve                                  # validate + file into library + catalogue + publish
y2y agol-sync status                                  # clean = published; unpublished = push it by hand
```
Sheet colours: **blue** = you fill, goes to ArcGIS Online · **orange** = you fill, stays in the catalogue · **grey / mauve** = don't touch (lighter shade = optional; see the `legend` tab). `tags`: separate with `;`, or enter `NA` for none. `category` must be one of the 10 exact names (reference §2). Rasters need `classification`: `continuous` or `categorical`.

### Edit / move / remove
```powershell
y2y update <id> --set summary="…" --set tags="a;b;c"   # change metadata fields
y2y rename <id> "Water/streams_v2.gpkg"                # move within library/
y2y refresh <id>                                       # re-stat after in-place edit
y2y update <id> --set status=deprecated                # retire: off AGOL, file + row kept
y2y tombstone <id> --reason "…"                        # soft-delete + erase file, off AGOL
```

### Check integrity
```powershell
y2y reconcile                # fast: catalogue ↔ filesystem
y2y reconcile --deep         # recompute checksums
y2y export-xlsx              # re-render inventory.xlsx (usually automatic)
```

### Publish to AGOL
```powershell
y2y agol-sync status                         # counts of active rows by sync state
y2y agol-sync push <id> [--dry-run]          # publish/update one row
y2y agol-sync push --all-unpublished         # initial bulk publish of the backlog
y2y agol-sync push --all-dirty               # push every pending_push row
y2y agol-sync restyle <id>                   # reset one layer to the default style (backs up the old one)
y2y agol-sync reconcile                      # weekly: push pending, flag drift, remove retired, audit the Atlas group
```

### Resolve AGOL drift
```powershell
y2y agol-sync pull <id>             # show diff, mark conflict
y2y agol-sync pull <id> --accept   # catalogue absorbs AGOL's text fields
y2y agol-sync pull <id> --reject   # re-push catalogue, overwrite AGOL
y2y agol-sync unpublish <id>       # take an active dataset off AGOL; keep row
```

---

**`sync_status`:** `unpublished` → `clean` ⇄ `pending_push` / `pending_pull` → `conflict` / `error`
**`agol_format`:** `feature-layer` (vector) · `imagery-layer` (raster) · `vector-tile-layer` (vector + VTPK)
**Auto-sync:** edits & approvals publish to AGOL straight away when logged in. Deprecating or tombstoning deletes the layer from AGOL (asks first).
**Weekly:** `y2y reconcile`, then `y2y agol-sync reconcile`. Keep a copy of `inventory\inventory.db` outside OneDrive.

> **Tip:** `y2y <command> --help` shows every option. First-time AGOL setup: `y2y agol-sync login` then `y2y agol-sync init-categories` (see reference doc §5).
