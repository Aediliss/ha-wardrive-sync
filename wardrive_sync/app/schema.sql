-- Wardrive Sync schema. Idempotent: applied on every add-on start.
-- Design: Wardrive Go keeps ONE row per device (MAC/key) and overwrites it.
-- Every sync sends the whole DB, so we store a new *version* of a row only
-- when something about it changed. That turns the app's "latest state" table
-- into a change history you can query over time.

CREATE SCHEMA IF NOT EXISTS wardrive;
SET search_path = wardrive, public;

-- One row per accepted upload (deduplicated by SHA-256 of the zip).
CREATE TABLE IF NOT EXISTS uploads (
    id              bigserial PRIMARY KEY,
    sha256          text        NOT NULL UNIQUE,
    received_at     timestamptz NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    label           text        NOT NULL,          -- X-Wardrive-Label (phone name)
    device          text,                          -- X-Wardrive-Device (hardware model)
    app_version     text,
    schema_version  int,
    hdr_networks    int,
    hdr_captures    int,
    bytes           bigint,
    remote_ip       text,
    manifest        jsonb,
    archive_path    text,
    stats           jsonb
);
CREATE INDEX IF NOT EXISTS uploads_label_received_idx ON uploads (label, received_at);

-- Versioned copy of the app's `observations` table (WiFi, BLE, cell summary rows).
CREATE TABLE IF NOT EXISTS observation_versions (
    id               bigserial PRIMARY KEY,
    key              text   NOT NULL,     -- BSSID / BLE MAC / cell id
    type             text   NOT NULL,     -- WIFI | BLE | CELL
    label            text   NOT NULL,     -- which phone reported it
    row_hash         text   NOT NULL,     -- hash of the fields that matter
    first_upload_id  bigint NOT NULL REFERENCES uploads (id),
    name             text,
    maker            text,
    category         text,
    icon             text,
    best_rssi        int,
    last_rssi        int,
    times_seen       int,
    first_seen       timestamptz,
    last_seen        timestamptz,
    company_id       int,
    channel          int,
    frequency        int,
    capabilities     text,
    connectable      boolean,
    lat              double precision,
    lng              double precision,
    altitude         double precision,
    accuracy         double precision,
    wifi_standard    int,
    via_monitor      boolean,
    notable          boolean,
    run_id           bigint,
    extra            jsonb,               -- app bookkeeping + any columns added in future app versions
    UNIQUE (label, key, row_hash)
);
CREATE INDEX IF NOT EXISTS ov_key_seen_idx      ON observation_versions (key, last_seen DESC);
CREATE INDEX IF NOT EXISTS ov_category_idx      ON observation_versions (category);
CREATE INDEX IF NOT EXISTS ov_company_idx       ON observation_versions (company_id);
CREATE INDEX IF NOT EXISTS ov_name_idx          ON observation_versions (name);
CREATE INDEX IF NOT EXISTS ov_first_seen_idx    ON observation_versions (first_seen);
CREATE INDEX IF NOT EXISTS ov_latlng_idx        ON observation_versions (lat, lng);

-- Every other table in the app DB (cell_tower, gnss_sat, runs, pmkids, ...),
-- versioned the same way but stored as JSON so new app schemas never break ingest.
CREATE TABLE IF NOT EXISTS snapshot_rows (
    id               bigserial PRIMARY KEY,
    table_name       text   NOT NULL,
    label            text   NOT NULL,
    pk               text   NOT NULL,
    row_hash         text   NOT NULL,
    first_upload_id  bigint NOT NULL REFERENCES uploads (id),
    data             jsonb  NOT NULL,
    UNIQUE (table_name, label, pk, row_hash)
);
CREATE INDEX IF NOT EXISTS sr_table_pk_idx ON snapshot_rows (table_name, pk);

-- Extra files in the zip (mesh_store.json, adsb_pending.json, ios_*.json, uploads/*).
-- prefs.json is never stored: it holds your WiGLE / WDGWars API keys.
CREATE TABLE IF NOT EXISTS sidecar_files (
    id               bigserial PRIMARY KEY,
    label            text   NOT NULL,
    name             text   NOT NULL,
    sha256           text   NOT NULL,
    bytes            bigint NOT NULL,
    first_upload_id  bigint NOT NULL REFERENCES uploads (id),
    data             jsonb,               -- parsed when it is JSON and under the size cap
    UNIQUE (label, name, sha256)
);

-- What counts as "surveillance gear". Edit freely; the views pick it up.
-- Any non-null column on a rule must match (ILIKE for patterns, = for company_id).
CREATE TABLE IF NOT EXISTS watch_rules (
    id                bigserial PRIMARY KEY,
    rule              text NOT NULL UNIQUE,
    category_pattern  text,
    name_regex        text,
    maker_pattern     text,
    company_id        int,
    obs_type          text,
    note              text
);
INSERT INTO watch_rules (rule, category_pattern, name_regex, maker_pattern, company_id, obs_type, note) VALUES
    ('flock-category',      '%flock%',     NULL,          NULL,     NULL, NULL,  'App-classified Flock camera'),
    ('flock-ble-signature', NULL,          '^[0-9]{10}$', NULL,     2504, 'BLE', 'Flock BLE beacon: company 2504 + 10-digit name, catches ones the app missed'),
    ('flock-maker',         NULL,          NULL,          'flock%', NULL, NULL,  'Maker string says Flock'),
    ('axon',                '%axon%',      NULL,          NULL,     NULL, NULL,  'Axon body cam / fleet'),
    ('drone',               '%drone%',     NULL,          NULL,     NULL, NULL,  'DJI and other drones'),
    ('camera-generic',      '%camera%',    NULL,          NULL,     NULL, NULL,  'Any category containing camera'),
    ('flock-name',          NULL,          '(?i)^flock-[0-9a-f]{4,}$', NULL, NULL, NULL, 'Flock-<hex> SSID/name (community-reported, verify)'),
    ('penguin-name',        NULL,          '(?i)^penguin-[0-9a-f]{4,}$', NULL, NULL, NULL, 'Penguin-<hex> names (community-reported Flock pattern, verify)'),
    ('fs-ext-battery',      NULL,          '(?i)fs ext battery', NULL, NULL, NULL, 'Flock battery pack BLE name (community-reported, verify)')
ON CONFLICT (rule) DO NOTHING;
