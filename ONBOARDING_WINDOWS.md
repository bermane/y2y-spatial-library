# Y2Y Spatial Library — Windows Onboarding (for Brynn)

This gets the pipeline installed and running on a Windows machine, connects it
to AGOL, and walks through publishing your first layer. Follow it top to bottom.

- **Quick daily reference:** `CHEATSHEET.html` (open in a browser).
- **Every command + option:** `COMMAND_REFERENCE.html`.
- **Operating rules (important — SharePoint/OneDrive):** `DEPLOYMENT.md`.

Commands are PowerShell. Run PowerShell from the **Start menu → "Windows
PowerShell"**. `>` marks a line you type.

---

## 1. Install the prerequisites (one time)

1. **Python 3.12** — https://www.python.org/downloads/
   During install, **tick "Add python.exe to PATH."** Verify:
   ```powershell
   > py -3.12 --version
   ```
   Should print `Python 3.12.x`.

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

This creates an isolated environment (`.venv`) and installs everything. It ends
with smoke tests — you should see `OK: all core imports succeeded`. (First run
downloads a few hundred MB; give it several minutes.)

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
and the catalogue (`inventory\inventory.db`) is created there on first use.

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

---

## 5. Your first ingest (the tutorial)

**Each work session** starts by activating the local venv and pointing the
pipeline at the SharePoint data folder. Set a `$root` variable once so you don't
retype the long path, and **pause OneDrive sync** before running commands:

```powershell
> C:\Y2Y\y2y-spatial-library\.venv\Scripts\Activate.ps1
> $root = "C:\Users\<you>\OneDrive - ...\Y2Y_Spatial_Library"
# (pause OneDrive sync now: system tray -> OneDrive -> gear -> Pause syncing)
```

The catalogue starts empty. Each layer goes **scan → review → approve →
publish**. Repeat for each tutorial layer.

1. **Drop a source file** (a `.gpkg`, `.shp`, `.tif`, …) into the data folder's
   `queue\incoming\`.

2. **Scan** — the pipeline inspects it and stages a review sheet:
   ```powershell
   > y2y --root $root ingest
   ```

3. **Review** — open the sheet, fill the metadata, mark it ready:
   ```powershell
   > start "$root\queue\processing\pending.xlsx"
   ```
   - Fill the required fields: `title`, `summary`, `description`, `tags`
     (semicolon-separated), `terms_of_use`, `acknowledgements`, `data_steward`.
     (`category` and `agol_format` are pre-filled — adjust if needed.)
   - You can **copy-paste** these from the existing inventory spreadsheet Ethan
     has open (same column names).
   - Set **`ready` = TRUE**.
   - **Save and CLOSE Excel** (approve fails if the file is still open).

4. **Approve** — validate + file into the library + catalogue:
   ```powershell
   > y2y --root $root ingest --approve
   ```

5. **Publish to AGOL:**
   ```powershell
   > y2y --root $root agol-sync status              # find the dataset_id
   > y2y --root $root agol-sync push <dataset_id>   # add --dry-run first to preview
   ```
   Then open the item in ArcGIS Online to confirm it's in your content with the
   right category, sharing, and thumbnail.

> Every `y2y` command takes `--root $root`. (If you'd rather not type it each
> time, `cd $root` first — `--root` then defaults to the current folder.)

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

---

## 7. Operating rules (read `DEPLOYMENT.md`)

The catalogue is a live SQLite database. To keep it safe:

- **Pause OneDrive/SharePoint sync while running `y2y` commands**, resume after.
  Never let the live `inventory.db` sync mid-write.
- **One machine only** operates the catalogue.
- **Sync is not backup** — keep an occasional separate copy of
  `inventory\inventory.db`.

Full rationale and a set-and-forget option are in `DEPLOYMENT.md`.

---

## Quick help

- `y2y --help` and `y2y <command> --help` — every command and option.
- `CHEATSHEET.html` — the everyday command subset (open in a browser).
- `COMMAND_REFERENCE.html` — the full reference with lookup tables.
