# Y2Y Spatial Library — Windows Onboarding (for Brynn)

This gets the pipeline installed and running on a Windows machine, connects it
to ArcGIS Online (AGOL), and walks through publishing your first layer. Follow it top to bottom.

- **Quick daily reference:** `CHEATSHEET.html` (open in a browser).
- **Every command + option:** `COMMAND_REFERENCE.html`.
- **Operating rules (important — SharePoint/OneDrive):** `DEPLOYMENT.md`.

Commands are PowerShell. Run PowerShell from the **Start menu → "Windows
PowerShell"**. `>` marks a line you type.

---

## 1. Install the prerequisites (one time)

1. **Python 3.13** — https://www.python.org/downloads/
   During install, **tick "Add python.exe to PATH."** Verify:
   ```powershell
   > py -3.13 --version
   ```
   Should print `Python 3.13.x`. (Python 3.12 also works. **Do not use 3.14 or
   newer** — one of the mapping libraries has no build for it yet, and the
   install fails.)

2. **Git** — https://git-scm.com/download/win  (accept defaults).
   *(Or install **GitHub Desktop** — https://desktop.github.com — if you prefer
   clicking "Clone"/"Pull" over typing git commands. Either works.)*

3. **ArcGIS Pro** — you already have it. It is **not** needed to run this
   pipeline; you only use it separately to build a `.vtpk` when a layer is
   published as a *vector tile layer* (rare). Keep it out of this workflow.

---

## 2. Get the code (on a LOCAL path, not SharePoint)

The **code** lives on a short local path; the **data** (library + catalogue)
lives in SharePoint (set up in §3b). Keeping the code — and especially its
`.venv` — out of SharePoint avoids two real problems: Windows' 260-character
path limit (a long SharePoint path + a deep `.venv` breaks installs) and
syncing hundreds of MB of machine-specific files into the shared library.

The repository is public, so no login is needed.

**With Git (PowerShell):**
```powershell
> mkdir C:\Y2Y
> cd C:\Y2Y
> git clone https://github.com/bermane/y2y-spatial-library.git
> cd y2y-spatial-library
```

**Or with GitHub Desktop:** *File → Clone repository →* paste the URL, and set
the **Local path** to `C:\Y2Y` (not a OneDrive/SharePoint folder).

> If you previously cloned into a SharePoint folder, delete that copy — the
> code should not live there.

---

## 3. Install the pipeline

From inside `C:\Y2Y\y2y-spatial-library`:

```powershell
> powershell -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
```

This creates an isolated environment (`.venv`) and installs everything, at the
tested package versions pinned in `constraints-windows.txt`. It then runs smoke
tests — you should see `OK: all core imports succeeded` — and the full test
suite, which takes a couple of minutes, uses temporary folders only, and never
touches your data or ArcGIS Online. It should end with
`Setup complete - all tests passed`. If it reports failed tests instead, stop
and send the output to Ethan. (First run downloads a few hundred MB; give it
several minutes.)

**Activate the environment** (do this in each new PowerShell window before
running `y2y`):
```powershell
> .\.venv\Scripts\Activate.ps1
> y2y --help
```

> If activation is blocked by execution policy, run once:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` and try again.

---

## 3b. Set up the data folder (in SharePoint)

The **data** — the `library/` files, the `inventory/` catalogue, and the
`queue/` — lives in your SharePoint folder so the library is shared with the
team. The pipeline reaches it via a `--root` pointer, so the local code and the
SharePoint data stay cleanly separated.

From inside `C:\Y2Y\y2y-spatial-library`, scaffold the data folder once (point
it at your SharePoint library location):

```powershell
> powershell -ExecutionPolicy Bypass -File .\scripts\new_data_root.ps1 `
    -DataRoot "C:\Users\<you>\OneDrive - ...\Y2Y_Spatial_Library"
```

That creates `library/` (with the category folders), `queue/`, `inventory/`,
and `reports/` at that path. You drop source files into its `queue\incoming\`,
and the catalogue (`inventory\inventory.db`) is created there the first time
you approve a dataset.

**Set the data folder once so you never retype it.** The pipeline reads a
`Y2Y_ROOT` environment variable as its default data root. Set it as a permanent
User variable via **Start → "Edit environment variables for your account" →
New…**, name `Y2Y_ROOT`, value = the same SharePoint path you used above. Reopen
PowerShell afterward. From then on, every `y2y` command uses it automatically —
no `--root`, no path retyping.

> **The one sync rule:** the live `inventory.db` sits in SharePoint, so
> **pause OneDrive/SharePoint sync while running `y2y` commands** and resume
> after (system tray → OneDrive → gear → *Pause syncing*). The `library/` files
> themselves are static after ingest and sync fine. Full rationale in
> `DEPLOYMENT.md`.

---

## 4. Connect to AGOL

You publish to **your own** login in the shared Y2Y Conservation Atlas org. This
uses named-user OAuth: a registered **OAuth application** (identified by a
"client id") is the conduit, and *you* are the identity that signs in.

### 4a. Register your own OAuth application (one time, ~5 minutes)

You want an app **you own**, so the integration doesn't depend on anyone else's
account. In ArcGIS Online (signed in as yourself):

1. **Content → New item → Developer credentials.**
   *(Older orgs: **New item → Application**, then open its Settings.)*
2. Choose **OAuth 2.0**.
3. Add a **Redirect URL** of exactly:
   ```
   urn:ietf:wg:oauth:2.0:oob
   ```
   This is the critical setting — it's the "out-of-band" redirect the Python
   login flow uses. Without it, `login` fails.
4. Save, then copy the **Client ID** (also called App ID) — a short string like
   `aB3xY…`.

> If Ethan hands you a client id instead, you can use that to get started — but
> registering your own (above) is the durable choice, since a client id tied to
> someone else's account breaks if that account is ever removed.

### 4b. Set the client id + log in

1. Set the client id from step 4a. For the current window:
   ```powershell
   > $env:Y2Y_AGOL_CLIENT_ID = "PASTE_YOUR_CLIENT_ID_HERE"
   ```
   To make it permanent (so you don't retype it every session), set a User
   environment variable named `Y2Y_AGOL_CLIENT_ID` via **Start → "Edit
   environment variables for your account"**, then reopen PowerShell.

2. Log in (opens a browser once; sign in as **yourself**, then paste the code
   back into PowerShell):
   ```powershell
   > y2y agol-sync login
   ```

3. Confirm it worked:
   ```powershell
   > python -c "from pipeline import agol_config, agol_sync; g=agol_sync.get_gis(agol_config.load_config()); print('OK, logged in as', g.users.me.username)"
   ```
   Should print your username with no browser popping up.

> **AGOL org categories.** The org's category schema must match the current
> typology (10 categories; see README "Taxonomy"). This is set **once, org-wide**
> by someone with org-admin rights:
> ```powershell
> > y2y agol-sync init-categories --dry-run   # preview the changes
> > y2y agol-sync init-categories             # apply
> ```
> If Ethan (or another admin) has already run it for the current typology, you
> do **not** need to — check with him first. Re-running is only needed when the
> typology itself changes.

> **Authoritative badge.** Each push marks the ArcGIS Online item
> "Authoritative" when the dataset's `authoritative` column is `yes`. Your
> account needs to be an administrator, or have a role with the privilege to
> mark content as authoritative. If it does not, the push still works, the
> badge is skipped, and a note starting with `[agol]` appears in the dataset's
> `internal_notes`. Tell Ethan if you see that note.

---

## 5. Your first ingest (the tutorial)

**Each work session:** activate the local venv, then **pause OneDrive sync**
before running commands. (With `Y2Y_ROOT` set from §3b, `y2y` already knows where
the data lives — no path to type.)

```powershell
C:\Y2Y\y2y-spatial-library\.venv\Scripts\Activate.ps1
# pause OneDrive sync now: system tray -> OneDrive -> gear -> Pause syncing
```

The catalogue starts empty. Each layer goes **scan → review → approve**, and
approving publishes it. For your very first run, use a small throwaway file and
give it a title like `TEST - delete me`, because it will be visible to the
organization until you remove it in step 6.

1. **Drop a source file** into the data folder's `queue\incoming\`. Accepted:
   Shapefile (all its parts together), GeoPackage, GeoJSON, KML/KMZ, and
   single-band GeoTIFF. A file with several layers (a file geodatabase, a
   multi-layer GeoPackage) or no coordinate system is rejected, with the
   reason written next to it in `queue\rejected\`.

2. **Scan** — the pipeline inspects it and stages a review sheet:
   ```powershell
   y2y ingest
   ```

3. **Review** — open the sheet, fill the metadata, mark it ready:
   ```powershell
   start "$env:Y2Y_ROOT\queue\processing\pending.xlsx"
   ```
   - The header colours tell you what to touch: **blue** and **orange** columns
     are yours to fill (blue ones are published to ArcGIS Online), **grey** and
     **mauve** columns are filled automatically. A lighter shade means
     optional. The `legend` tab in the sheet explains them.
   - Fill: `title` (pre-filled with the file name, so rewrite it into a proper
     title), `summary`, `description`, `tags` (semicolon-separated, or `NA` if
     the dataset should have no tags), `terms_of_use`, `acknowledgements`,
     `data_steward`.
   - `category` is guessed from the file name and is often empty. It must be
     exactly one of:
     `Boundaries, Tenure & Governance` · `Biodiversity & Ecosystems` ·
     `Climate Change` · `Connectivity & Wildlife Movement` · `Species` ·
     `Water` · `Land Cover, Land Use & Disturbance` ·
     `Human Dimensions of Conservation` · `Threats & Infrastructure` ·
     `Demographics & Socioeconomic Data`.
     For `Species` only, also fill `subcategory`: `Caribou`, `Elk`, `Goat`,
     `Grizzly Bear`, `Multi-Species`, `Wolverine` or `Other`.
   - For a raster (`.tif`), fill `classification`: `continuous` (elevation, a
     habitat score) or `categorical` (land cover classes). Vectors are
     pre-filled.
   - `agol_format`, `authoritative`, `target_filename` and `status` are
     pre-filled; adjust if needed. Leave `agol_item_id` empty.
   - Set **`ready` = TRUE**.
   - **Save and CLOSE Excel** (approve fails if the file is still open).

4. **Approve** — validate, file into the library and catalogue, and publish:
   ```powershell
   y2y ingest --approve
   ```
   If a row fails, its `ready` goes back to FALSE and the `_validation_error`
   column says why. Fix it, set `ready` to TRUE and approve again.

   Because you are signed in to ArcGIS Online, approving also **publishes the
   layer straight away**, shared with the organization and the Y2Y
   Conservation Atlas group. The command does not print the result of that
   publish, so check it:
   ```powershell
   y2y agol-sync status
   ```
   `clean` means it is published. `unpublished` means the publish did not go
   through; run the push yourself to see why (next step).

5. **Find the dataset's ID, and push by hand when needed.** Every dataset has
   an ID like `ds_01ABC...`. Look it up in the `dataset_id` column of
   `inventory\inventory.xlsx` in the data folder (a read-only view, rewritten
   after each command; close it before running `y2y`).
   ```powershell
   start "$env:Y2Y_ROOT\inventory\inventory.xlsx"
   y2y agol-sync push <dataset_id> --dry-run   # preview
   y2y agol-sync push <dataset_id>             # publish, or show the error
   ```
   Then open the item in ArcGIS Online and confirm:
   - it is in your content, in the right category folder, with a thumbnail;
   - it is shared with the organization and the Y2Y Conservation Atlas group;
   - it shows the **Authoritative** badge (when `authoritative` is `yes`);
   - for a feature layer, **Settings → "Allow others to export to different
     formats"** is ticked. (Vector tile and imagery layers are view-only.)

   If the badge is missing, see the Authoritative note in §4.

6. **Remove the test layer.** Retiring a dataset takes its layer off ArcGIS
   Online:
   ```powershell
   y2y update <dataset_id> --set status=deprecated
   ```
   It warns you, asks you to confirm, then permanently deletes the layer from
   ArcGIS Online. The library file and the catalogue row are kept. Check that
   the item is gone from your content. To also erase the test file from the
   library, run `y2y tombstone <dataset_id> --reason "install test"` (this
   cannot be undone; the catalogue row stays as a record).

> **Later edits publish themselves too.** `y2y update`, `y2y rename` and
> `y2y refresh` on a published dataset push the change to ArcGIS Online right
> away. If ArcGIS Online cannot be reached, the catalogue change still
> succeeds and the weekly reconcile (§7) catches up.

> `y2y` uses `Y2Y_ROOT` automatically. To override for a one-off (e.g. testing
> against a different folder), pass `--root "C:\some\other\path"`.

---

## 6. Getting updates from Ethan

Ethan pushes code updates to GitHub; you pull them. Your **data is never
touched** by an update (it lives outside git).

- **Get the new code:** GitHub Desktop → **Fetch/Pull origin** (one click), or:
  ```powershell
  > git pull
  ```
- **Reconcile dependencies** (safe to run every time; only does work when a
  release changed dependencies):
  ```powershell
  > powershell -ExecutionPolicy Bypass -File .\scripts\update_windows.ps1
  ```

Because the pipeline is installed in "editable" mode, a pull usually makes the
new code live immediately — the script just covers the occasional case where
dependencies changed.

- **Check which version you are on** (do this after every update, and quote it
  when you report a problem to Ethan):
  ```powershell
  > y2y --version
  ```
  It should match the newest entry in `CHANGELOG.md`, which also lists what
  changed in each release.

---

## 7. Weekly routine and operating rules (read `DEPLOYMENT.md`)

**Once a week**, with OneDrive sync paused:

```powershell
y2y reconcile                 # catalogue vs. files in the library
y2y agol-sync reconcile       # catalogue vs. ArcGIS Online
```

The second command retries anything that did not publish, flags layers that
were edited on ArcGIS Online, removes layers of retired datasets, and audits
the Y2Y Conservation Atlas group. Each writes a report into the data folder's
`reports\`. On a new install the group audit lists every layer already in the
group as "not tracked"; that is expected, and it changes nothing. There is no
scheduled task for this on Windows yet, so run it by hand.

The catalogue is a live SQLite database. To keep it safe:

- **Pause OneDrive/SharePoint sync while running `y2y` commands**, resume after.
  Never let the live `inventory.db` sync mid-write.
- **One machine only** operates the catalogue.
- **Sync is not backup** — keep an occasional separate copy of
  `inventory\inventory.db`, outside the synced folder:
  ```powershell
  mkdir C:\Y2Y\backups -Force
  Copy-Item "$env:Y2Y_ROOT\inventory\inventory.db" "C:\Y2Y\backups\inventory_$(Get-Date -Format yyyy-MM-dd).db"
  ```

Full rationale is in `DEPLOYMENT.md`.

---

## Quick help

- `y2y --help` and `y2y <command> --help` — every command and option.
- `CHEATSHEET.html` — the everyday command subset (open in a browser).
- `COMMAND_REFERENCE.html` — the full reference with lookup tables.
