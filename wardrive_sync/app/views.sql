-- Views are dropped and recreated on every start so they always match the
-- current tables (including the PostGIS geom column once it is enabled).
SET search_path = wardrive, public;
DROP VIEW IF EXISTS surveillance_site_devices, surveillance_sites, cell_tower_history,
    device_changes, surveillance_devices, latest_observations, observation_history CASCADE;

-- Every version with the time it reached the server and from which phone.
CREATE VIEW observation_history AS
SELECT v.*, u.received_at AS synced_at, u.device
FROM observation_versions v
JOIN uploads u ON u.id = v.first_upload_id;

-- Latest known state of each device, merged across phones.
CREATE VIEW latest_observations AS
SELECT DISTINCT ON (key) *
FROM observation_versions
ORDER BY key, last_seen DESC NULLS LAST, id DESC;

-- Devices matching any watch rule (latest state), with the rules that matched.
CREATE VIEW surveillance_devices AS
SELECT l.*, m.rules
FROM latest_observations l
CROSS JOIN LATERAL (
    SELECT array_agg(r.rule ORDER BY r.rule) AS rules
    FROM watch_rules r
    WHERE (r.category_pattern IS NOT NULL OR r.name_regex IS NOT NULL
           OR r.maker_pattern IS NOT NULL OR r.company_id IS NOT NULL)
      AND (r.category_pattern IS NULL OR l.category ILIKE r.category_pattern)
      AND (r.name_regex       IS NULL OR l.name ~ r.name_regex)
      AND (r.maker_pattern    IS NULL OR l.maker ILIKE r.maker_pattern)
      AND (r.company_id       IS NULL OR l.company_id = r.company_id)
      AND (r.obs_type         IS NULL OR l.type = r.obs_type)
) m
WHERE m.rules IS NOT NULL;

-- Field-level changes for one device over time (name, security, channel,
-- vendor id, position). First version of each device shows as change_kind 'new'.
CREATE VIEW device_changes AS
SELECT key, type, label, synced_at, last_seen, category,
       CASE WHEN prev_id IS NULL THEN 'new' ELSE 'changed' END AS change_kind,
       prev_name, name,
       prev_capabilities, capabilities,
       prev_channel, channel,
       prev_company_id, company_id,
       prev_maker, maker,
       round((111320 * sqrt(power(lat - prev_lat, 2)
             + power((lng - prev_lng) * cos(radians(lat)), 2)))::numeric, 1) AS moved_m
FROM (
    SELECT h.*,
           lag(id)           OVER w AS prev_id,
           lag(name)         OVER w AS prev_name,
           lag(capabilities) OVER w AS prev_capabilities,
           lag(channel)      OVER w AS prev_channel,
           lag(company_id)   OVER w AS prev_company_id,
           lag(maker)        OVER w AS prev_maker,
           lag(lat)          OVER w AS prev_lat,
           lag(lng)          OVER w AS prev_lng
    FROM observation_history h
    WINDOW w AS (PARTITION BY key ORDER BY last_seen NULLS FIRST, id)
) x
WHERE prev_id IS NULL
   OR name         IS DISTINCT FROM prev_name
   OR capabilities IS DISTINCT FROM prev_capabilities
   OR channel      IS DISTINCT FROM prev_channel
   OR company_id   IS DISTINCT FROM prev_company_id
   OR maker        IS DISTINCT FROM prev_maker
   OR (lat IS NOT NULL AND prev_lat IS NOT NULL
       AND 111320 * sqrt(power(lat - prev_lat, 2)
           + power((lng - prev_lng) * cos(radians(lat)), 2)) > 50);

-- Cell towers as their own versioned history (from snapshot_rows).
CREATE VIEW cell_tower_history AS
SELECT s.pk AS id, s.label, u.received_at AS synced_at,
       s.data->>'tech' AS tech, s.data->>'operator' AS operator,
       s.data->>'mcc' AS mcc, s.data->>'mnc' AS mnc,
       (s.data->>'cid')::bigint AS cid, (s.data->>'pci')::int AS pci,
       (s.data->>'tac')::int AS tac, (s.data->>'arfcn')::int AS arfcn,
       (s.data->>'best_dbm')::int AS best_dbm,
       to_timestamp((s.data->>'first_seen')::bigint / 1000.0) AS first_seen,
       to_timestamp((s.data->>'last_seen')::bigint / 1000.0)  AS last_seen,
       (s.data->>'times_seen')::int AS times_seen
FROM snapshot_rows s JOIN uploads u ON u.id = s.first_upload_id
WHERE s.table_name = 'cell_tower';
