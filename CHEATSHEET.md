# Y2Y Spatial Library — One-Page Cheat Sheet

Everyday commands. Full detail in `COMMAND_REFERENCE.md`. Design in `DESIGN.md`.

```bash
# START OF SESSION (run once per new terminal)
cd /path/to/Spatial_Data && source .venv/bin/activate
```

`inventory/inventory.db` is the source of truth. The xlsx is a read-only view. AGOL is downstream.

---

### Ingest new data  ·  scan → review → approve
```bash
y2y ingest                              # scan queue/incoming/ → pending.xlsx
open queue/processing/pending.xlsx      # fill fields, set ready=TRUE
y2y ingest --approve                    # validate + promote into library/ + catalogue
```
Sheet colours: **blue** = you fill, goes to ArcGIS Online · **orange** = you fill, stays in the catalogue · **grey / mauve** = don't touch (lighter shade = optional; see the `legend` tab). `tags`: separate with `;`, or enter `NA` for none.

### Edit / move / remove
```bash
y2y update <id> --set summary="…" --set tags="a;b;c"   # change metadata fields
y2y rename <id> "Water/streams_v2.gpkg"                # move within library/
y2y refresh <id>                                       # re-stat after in-place edit
y2y update <id> --set status=deprecated                # retire: off AGOL, file + row kept
y2y tombstone <id> --reason "…"                        # soft-delete + erase file, off AGOL
```

### Check integrity
```bash
y2y reconcile                # fast: catalogue ↔ filesystem
y2y reconcile --deep         # recompute checksums
y2y export-xlsx              # re-render inventory.xlsx (usually automatic)
```

### Publish to AGOL
```bash
y2y agol-sync status                         # sync state of every active row
y2y agol-sync push <id> [--dry-run]          # publish/update one row
y2y agol-sync push --all-unpublished         # initial bulk publish of the backlog
y2y agol-sync push --all-dirty               # push every pending_push row
y2y agol-sync restyle <id>                   # reset one layer to the default style (backs up the old one)
y2y agol-sync reconcile                      # weekly: push pending, flag drift, remove retired, audit the Atlas group
```

### Resolve AGOL drift
```bash
y2y agol-sync pull <id>             # show diff, mark conflict
y2y agol-sync pull <id> --accept   # catalogue absorbs AGOL's text fields
y2y agol-sync pull <id> --reject   # re-push catalogue, overwrite AGOL
y2y agol-sync unpublish <id>       # take an active dataset off AGOL; keep row
```

---

**`sync_status`:** `unpublished` → `clean` ⇄ `pending_push` / `pending_pull` → `conflict` / `error`
**`agol_format`:** `feature-layer` (vector) · `imagery-layer` (raster) · `vector-tile-layer` (vector + VTPK)
**Auto-sync:** edits & approvals auto-push when logged in (`Y2Y_AGOL_AUTO_PUSH=false` to disable).

> **Tip:** `y2y <command> --help` shows every option. First-time AGOL setup: `y2y agol-sync login` then `y2y agol-sync init-categories` (see reference doc §5).
