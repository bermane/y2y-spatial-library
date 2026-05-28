-- ============================================================================
-- Y2Y Spatial Library — SQLite schema
-- ============================================================================
--
-- Source-of-truth metadata catalogue. Replaces inventory.xlsx, which becomes
-- a generated read-only view via `y2y export-xlsx`.
--
-- Design priorities:
--   * PostGIS-portable: lowercase identifiers, TEXT for strings, INTEGER for
--     bools (0/1), ISO-8601 UTC timestamps with explicit 'Z' suffix.
--   * STRICT mode on every table: catches type errors that would block a
--     Postgres import.
--   * Foreign keys declared in DDL. Application MUST run
--     `PRAGMA foreign_keys = ON` on every connection (handled by
--     pipeline/db.py:get_connection()).
--   * Single-table catalogue today (`datasets`); field-group comments
--     document the eventual per-type split into an extension table (e.g.
--     `spatial_datasets`) if the library expands beyond spatial.
--
-- See DESIGN.md for the full rationale.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- datasets — the catalogue. One row per dataset.
-- ----------------------------------------------------------------------------
-- Field groups:
--   [type-agnostic]    columns expected to stay on `datasets` if the
--                      catalogue expands to non-spatial types.
--   [spatial-specific] columns that should move into a per-type extension
--                      table (e.g. `spatial_datasets`) at that future point.
--
-- Conventions:
--   * dataset_id format: 'ds_<26-char Crockford base32 ULID>'.
--   * All timestamps are ISO-8601 UTC with explicit 'Z' suffix.
--   * Categorical text fields use display names (with spaces, ampersands)
--     verbatim from Spatial_Data_Typology.xlsx; the on-disk folder names
--     are mapped from these by pipeline/taxonomy.py.
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS datasets (
    -- [type-agnostic] Identity & taxonomy --------------------------------
    dataset_id              TEXT PRIMARY KEY
                                CHECK (dataset_id LIKE 'ds_%'),
    dataset_type            TEXT NOT NULL DEFAULT 'spatial'
                                CHECK (dataset_type IN ('spatial')),
    title                   TEXT NOT NULL,
    -- Single-category invariant (Phase D.6, 2026-05-28):
    --   * Catalogue: one row → one `category` (and optional one
    --     `subcategory`); SQLite enforces single-valued TEXT.
    --   * Library:   the file lives in exactly one category folder
    --     (under `library/spatial/<category>/[<subcategory>/]`);
    --     changing it requires `y2y rename` (a file move).
    --   * AGOL:      the item is assigned to exactly one top-level
    --     category. agol_sync.compute_item_properties() sends a
    --     single hierarchical-path entry ('Species/Caribou'), not
    --     two flat entries — so subcategorised items still get
    --     one top-level membership. AGOL's Item.update() with the
    --     full categories list REPLACES; even if a steward adds
    --     extras in the Map Viewer UI, the next push overwrites
    --     back to one.
    -- Multi-rendition (one dataset → multiple AGOL items, each in
    -- its own category) is deferred to v2 per the integration plan.
    category                TEXT NOT NULL
                                CHECK (category IN (
                                    -- Order matches Spatial_Data_Typology.xlsx
                                    -- (2026 director-workshop revision; see
                                    -- migration 006 for the rename diff).
                                    'Jurisdictional & Political Boundaries',
                                    'Land Designations & Tenure',
                                    'Biodiversity & Ecosystems',
                                    'Climate Resilience',
                                    'Connectivity & Wildlife Movement',
                                    'Species',
                                    'Water',
                                    'Land Cover, Land Use & Disturbance',
                                    'Human Dimensions',
                                    'Threats & Infrastructure'
                                )),
    subcategory             TEXT,                            -- nullable; only Species has subs (enforced in code, not schema)
    file_path               TEXT NOT NULL,                   -- relative to library/spatial/
    format                  TEXT NOT NULL
                                CHECK (format IN ('geopackage', 'geotiff')),

    -- [type-agnostic] Extrinsic metadata (Dublin Core+) ------------------
    -- description carries both the public-facing dataset description and
    -- any processing/lineage details (e.g. "Geometry repaired with
    -- ogr2ogr -makevalid; 5 ring self-intersections fixed").
    --
    -- == Future work: rich text vs plain text =============================
    -- summary, description, terms_of_use, and acknowledgements are
    -- currently stored as plain text. AGOL natively stores HTML for
    -- description / accessInformation / licenseInfo (the AGOL Map
    -- Viewer rich-text editor produces <p>…</p> wrappers, <a> links,
    -- lists, headings); snippet (summary) stays plain by AGOL
    -- convention. This creates two questions:
    --
    --   1. Should plain-text catalogue ↔ HTML-wrapped AGOL be treated
    --      as drift, or as semantically equivalent?
    --   2. Should the catalogue grow first-class support for rich
    --      text so steward-authored AGOL formatting (links, lists)
    --      survives a pull --accept?
    --
    -- Three tracks were considered (2026-05-28):
    --
    --   Track 1 — Semantic-equivalence diff (no schema change).
    --     agol_sync._diff_adoption_fields() normalises both sides
    --     (strip <p>/<br>/<div>/<span>, decode HTML entities,
    --     collapse whitespace) before comparing. Plain↔wrapped is no
    --     drift; meaningful tags (<a>, <ul>, …) are real drift.
    --     Rich text supported via an escape hatch — steward types
    --     HTML directly into the catalogue column; xlsx shows it
    --     verbatim. ~150 LOC; no migration.
    --
    --   Track 2 — Markdown catalogue, HTML wire format.
    --     Catalogue stores Markdown. Push renders → HTML; pull
    --     converts AGOL HTML → Markdown via html2text / markdownify.
    --     Readable xlsx; lossless for the formatting stewards
    --     typically use; lossy for non-canonical AGOL HTML (Map
    --     Viewer editor's vendor classes / <span> wrappers).
    --     Adds a Markdown / HTML-conversion dep; ~300 LOC.
    --
    --   Track 3 — Dual-column schema (plain + _html siblings).
    --     New columns: description_html, terms_of_use_html,
    --     acknowledgements_html. _html is authoritative when set;
    --     plain is denormalised for grep / xlsx. Migration 0XX adds
    --     the columns + every read/write path updated. Lossless;
    --     largest blast radius (~500 LOC + migration + UX docs).
    --
    -- Decision (2026-05-28): Track 1 in Phase D.4 — fix the diff
    -- false-positive (Fortress Mountain HTML wrapping flagged as
    -- conflict) and enable raw-HTML escape hatch. Defer Track 2 /
    -- Track 3 until a steward use case emerges that the escape
    -- hatch can't serve.
    --
    -- Revisit criteria for Track 2 or Track 3:
    --   • Steward wants to author formatting in the catalogue (CLI
    --     or xlsx) rather than the AGOL UI.
    --   • Multiple datasets carry rich content (links, lists) that
    --     the steward edits routinely.
    --   • xlsx readability becomes a sticking point with HTML-in-
    --     cells (Track 1's accepted tradeoff).
    -- ====================================================================
    summary                 TEXT NOT NULL,
    description             TEXT NOT NULL,
    tags                    TEXT NOT NULL,                   -- ';'-delimited
    terms_of_use            TEXT NOT NULL,
    acknowledgements        TEXT NOT NULL,
    data_steward            TEXT NOT NULL,
    internal_notes          TEXT,                            -- freeform; renamed from xlsx 'notes'

    -- [type-agnostic] Lifecycle state & timestamps -----------------------
    status                  TEXT NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'deprecated', 'tombstoned')),
    date_added              TEXT NOT NULL,                   -- ISO-8601 UTC with explicit 'Z'
    date_modified           TEXT NOT NULL,                   -- ISO-8601 UTC with explicit 'Z'

    -- [type-agnostic] AGOL linkage (see DESIGN.md §15). The catalogue
    -- ↔ AGOL state machine lives in pipeline/agol_sync.py.
    agol_item_id            TEXT,                            -- UNIQUE-when-not-null via partial index below
    agol_published_at       TEXT,                            -- ISO-8601 UTC
    last_synced_at          TEXT,                            -- ISO-8601 UTC
    sync_status             TEXT NOT NULL DEFAULT 'unpublished'
                                CHECK (sync_status IN (
                                    'clean',
                                    'pending_push',
                                    'pending_pull',
                                    'conflict',
                                    'error',
                                    'unpublished'
                                )),
    -- Steward-declared publish format. Drives which publish path
    -- agol_sync.push() takes:
    --   feature-layer       → arcgis SDK Item.publish() on uploaded GPKG
    --   vector-tile-layer   → upload steward-built VTPK + publish
    --                         (no intermediate hosted feature layer)
    --   imagery-layer       → arcgis.raster.publish_hosted_imagery_layer
    -- Pre-filled by ingest._build_row() from format; editable later
    -- via `y2y update <id> --set agol_format=...`. Added as
    -- agol_target by migration 007; renamed to agol_format by
    -- migration 008 (naming alignment with format / source_format).
    agol_format             TEXT
                                CHECK (agol_format IS NULL OR agol_format IN (
                                    'feature-layer',
                                    'vector-tile-layer',
                                    'imagery-layer'
                                )),

    -- [spatial-specific] Intrinsic snapshot (drift detection) ------------
    -- Captured at promote-time from the canonical file. Reconcile compares
    -- these to the file on disk; mismatch is "drift" (auto-resolved if the
    -- file still passes canonical validators, see DESIGN.md §2).
    checksum_sha256         TEXT NOT NULL,
    size_bytes              INTEGER NOT NULL,
    mtime                   TEXT NOT NULL,                   -- ISO-8601 UTC
    crs                     TEXT NOT NULL,                   -- canonical CRS (ESRI:102008 today)
    geographic_extent_bbox  TEXT,                            -- 'minx,miny,maxx,maxy' in canonical CRS
    classification          TEXT NOT NULL
                                CHECK (classification IN ('vector', 'continuous', 'categorical')),

    -- [spatial-specific] Spatial properties ------------------------------
    footprint_wkt           TEXT,                            -- bbox/footprint as WKT in EPSG:4326
    temporal_start          TEXT,                            -- ISO-8601 UTC
    temporal_end            TEXT,                            -- ISO-8601 UTC
    feature_count           INTEGER,                         -- vectors only
    raster_width            INTEGER,                         -- rasters only
    raster_height           INTEGER,                         -- rasters only
    pixel_size_x            REAL,                            -- rasters only
    pixel_size_y            REAL,                            -- rasters only

    -- [spatial-specific] Source provenance (locked at scan) --------------
    source_format           TEXT
                                CHECK (source_format IS NULL OR source_format IN (
                                    'shapefile', 'geopackage', 'geojson', 'kml', 'geotiff'
                                )),
    source_filename         TEXT,                            -- original filename in queue/incoming/
    source_crs              TEXT,                            -- raw CRS as captured (may be projection name without auth code)
    source_layer            TEXT                             -- layer name; null in Phase A (single-layer only)
) STRICT;


-- ----------------------------------------------------------------------------
-- changelog — append-only audit log of every catalogue mutation.
-- ----------------------------------------------------------------------------
-- ON DELETE RESTRICT prevents accidental dataset removal from orphaning
-- audit history. Tombstoning sets status='tombstoned'; rows are never
-- DELETE'd from `datasets` in normal operation.

CREATE TABLE IF NOT EXISTS changelog (
    id                      TEXT PRIMARY KEY
                                CHECK (id LIKE 'cl_%'),      -- 'cl_<26-char ULID>'
    timestamp               TEXT NOT NULL,                   -- ISO-8601 UTC with 'Z'
    dataset_id              TEXT NOT NULL
                                REFERENCES datasets(dataset_id) ON DELETE RESTRICT,
    action                  TEXT NOT NULL
                                CHECK (action IN (
                                    'add',
                                    'update',
                                    'rename',
                                    'remove',
                                    'refresh',
                                    'metadata',
                                    'reconcile-note',
                                    'migrated_from_xlsx',
                                    'id_format_migration'
                                )),
    field_changed           TEXT,                            -- nullable; null for row-level actions
    old_value               TEXT,
    new_value               TEXT,
    note                    TEXT,
    actor                   TEXT
) STRICT;


-- ----------------------------------------------------------------------------
-- schema_migrations — tracks which numbered migrations have been applied.
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_migrations (
    version                 TEXT PRIMARY KEY,                -- '001', '002', ...
    applied_at              TEXT NOT NULL,                   -- ISO-8601 UTC
    description             TEXT NOT NULL
) STRICT;


-- ----------------------------------------------------------------------------
-- Indexes
-- ----------------------------------------------------------------------------

-- Sparse UNIQUE on agol_item_id: enforces uniqueness only when set, so
-- many unpublished rows (NULL agol_item_id) can coexist.
CREATE UNIQUE INDEX IF NOT EXISTS idx_datasets_agol_item_id_unique
    ON datasets(agol_item_id) WHERE agol_item_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_datasets_category
    ON datasets(category);

CREATE INDEX IF NOT EXISTS idx_datasets_file_path
    ON datasets(file_path);

CREATE INDEX IF NOT EXISTS idx_datasets_checksum_sha256
    ON datasets(checksum_sha256);

CREATE INDEX IF NOT EXISTS idx_changelog_dataset_id_timestamp
    ON changelog(dataset_id, timestamp);
