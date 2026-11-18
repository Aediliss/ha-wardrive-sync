-- Starter queries for the wardrive database. Run in pgAdmin 4 (Expaso add-on)
-- or psql. Everything lives in the `wardrive` schema.
SET search_path = wardrive, public;

-- 1. Every surveillance device, newest first, with the rules that flagged it.
SELECT key, type, name, maker, category, company_id, first_seen, last_seen, lat, lng, rules
FROM surveillance_devices
ORDER BY first_seen DESC;

-- 2. Flock BLE beacons the app did NOT label as Flock (caught by signature:
--    company id 2504 + 10-digit name). These are the ones to look at closely.
SELECT key, name, category, first_seen, lat, lng
FROM surveillance_devices
WHERE 'flock-ble-signature' = ANY (rules) AND category NOT ILIKE '%flock%';

-- 3. Field-level changes on surveillance gear (rename, security, channel,
--    vendor id, moved > 50 m), in time order.
SELECT c.*
FROM device_changes c
JOIN surveillance_devices s USING (key)
WHERE c.change_kind = 'changed'
ORDER BY c.synced_at DESC;

-- 4. New surveillance devices that reached the server in the last 7 days.
SELECT h.key, h.name, h.category, h.synced_at, h.lat, h.lng
FROM observation_history h
JOIN surveillance_devices s USING (key)
WHERE h.synced_at > now() - interval '7 days'
  AND NOT EXISTS (SELECT 1 FROM observation_versions o
                  WHERE o.key = h.key AND o.id < h.id);

-- ---------------- PostGIS required below (see SETUP.md step 2) ----------------

-- 5. Sites where more than one MAC has appeared: candidate MAC rotation.
SELECT site_id, distinct_keys, names, categories, first_seen, last_seen, lat, lng
FROM surveillance_sites
WHERE distinct_keys > 1
ORDER BY distinct_keys DESC, last_seen DESC;

-- 6. Timeline of one site: which key was seen when.
--    Replace 42 with a site_id from query 5. (site_id numbering is recomputed
--    as data grows, so look it up fresh each time.)
SELECT key, name, category, first_seen, last_seen, best_rssi, times_seen, label
FROM surveillance_site_devices
WHERE site_id = 42
ORDER BY first_seen;

-- 7. Everything within 100 m of a point (replace <lng>, <lat>, e.g. -98.5, 39.8).
SELECT key, type, name, category, first_seen, last_seen,
       round(ST_Distance(geom::geography,
             ST_SetSRID(ST_MakePoint(<lng>, <lat>), 4326)::geography)) AS metres
FROM latest_observations
WHERE ST_DWithin(geom::geography,
                 ST_SetSRID(ST_MakePoint(<lng>, <lat>), 4326)::geography, 100)
ORDER BY metres;

-- ---------------- housekeeping ----------------

-- Sync log: one row per accepted upload.
SELECT id, received_at, label, device, app_version, schema_version, hdr_networks,
       stats->>'observations_new_versions' AS new_versions
FROM uploads ORDER BY id DESC LIMIT 50;

-- Add your own watch rule (any non-null column must match):
-- INSERT INTO watch_rules (rule, category_pattern, name_regex, maker_pattern, company_id, obs_type, note)
-- VALUES ('my-rule', NULL, '(?i)^raven', NULL, NULL, 'BLE', 'why');
