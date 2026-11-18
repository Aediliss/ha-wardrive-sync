-- Physical "sites": surveillance devices clustered by location.
-- A Flock pole that rotates its BLE MAC shows up as several keys at one site,
-- so a site with many keys over time is the signal you are looking for.
-- eps = 75 m (Wardrive Go stores one position per device, often from a weak
-- reading, so positions for the same pole can be tens of metres apart).
SET search_path = wardrive, public;

CREATE VIEW surveillance_site_devices AS
SELECT ST_ClusterDBSCAN(ST_Transform(geom, 5070), eps := 75, minpoints := 1) OVER () AS site_id,
       key, type, name, maker, category, company_id, capabilities, channel,
       best_rssi, times_seen, first_seen, last_seen, label, rules, lat, lng, geom
FROM surveillance_devices
WHERE geom IS NOT NULL;

CREATE VIEW surveillance_sites AS
SELECT site_id,
       count(DISTINCT key)                                   AS distinct_keys,
       array_agg(DISTINCT category)                          AS categories,
       array_agg(DISTINCT name) FILTER (WHERE name IS NOT NULL) AS names,
       array_agg(DISTINCT company_id) FILTER (WHERE company_id IS NOT NULL) AS company_ids,
       min(first_seen)                                       AS first_seen,
       max(last_seen)                                        AS last_seen,
       ST_Y(ST_Centroid(ST_Collect(geom)))                   AS lat,
       ST_X(ST_Centroid(ST_Collect(geom)))                   AS lng,
       ST_Centroid(ST_Collect(geom))                         AS center
FROM surveillance_site_devices
GROUP BY site_id;
