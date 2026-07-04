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

## 2. Get the code

The repository is public, so no login is needed.

**With Git (PowerShell):**
```powershell
> cd $HOME\Documents
> git clone https://github.com/bermane/y2y-spatial-library.git
> cd y2y-spatial-library
```

**Or with GitHub Desktop:** *File → Clone repository →* paste the URL above →
Clone, then open the folder location.

> **Where to put it:** pick a stable folder like `Documents\y2y-spatial-library`.
> See `DEPLOYMENT.md` for how this relates to your SharePoint/OneDrive setup —
> in short, the live catalogue must **not** be syncing while you run commands.

---

## 3. Install the pipeline

From inside the `y2y-spatial-library` folder:

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

The catalogue starts empty. Each layer goes **scan → review → approve →
publish**. Repeat for each tutorial layer.

1. **Drop a source file** (a `.gpkg`, `.shp`, `.tif`, …) into `queue\incoming\`.

2. **Scan** — the pipeline inspects it and stages a review sheet:
   ```powershell
   > y2y ingest
   ```

3. **Review** — open the sheet, fill the metadata, mark it ready:
   ```powershell
   > start .\queue\processing\pending.xlsx
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
   > y2y ingest --approve
   ```

5. **Publish to AGOL:**
   ```powershell
   > y2y agol-sync status                 # find the dataset_id (or note it from approve)
   > y2y agol-sync push <dataset_id>      # add --dry-run first to preview
   ```
   Then open the item in ArcGIS Online to confirm it's in your content with the
   right category, sharing, and thumbnail.

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
