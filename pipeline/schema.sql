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
    category                TEXT NOT NULL
                                CHECK (category IN (
                                    'Administrative & Jurisdictional Boundaries',
                                    'Biodiversity & Ecosystems',
                                    'Climate Resilience',
                                    'Connectivity & Wildlife Movement',
                                    'Species & Species at Risk',
                                    'Water',
                                    'Land Cover, Land Use & Disturbance',
                                    'Protected Areas & Conservation Lands',
                                    'Threats, Human Footprint & Infrastructure'
                                )),
    subcategory             TEXT,                            -- nullable; only Species has subs (enforced in code, not schema)
    file_path               TEXT NOT NULL,                   -- relative to library/spatial/
    format                  TEXT NOT NULL
                                CHECK (format IN ('geopackage', 'geotiff')),

    -- [type-agnostic] Extrinsic metadata (Dublin Core+) ------------------
    -- description carries both the public-facing dataset description and
    -- any processing/lineage details (e.g. "Geometry repaired with
    -- ogr2ogr -makevalid; 5 ring self-intersections fixed").
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

    -- [type-agnostic] AGOL linkage (reserved; not populated this session) -
    -- See DESIGN.md "AGOL integration" placeholder section.
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
