-- Applied only when the postgis extension is installed in this database
-- (CREATE EXTENSION postgis; as the postgres superuser, see SETUP.md).
SET search_path = wardrive, public;

ALTER TABLE observation_versions
    ADD COLUMN IF NOT EXISTS geom geometry(Point, 4326)
    GENERATED ALWAYS AS (
        CASE WHEN lat IS NOT NULL AND lng IS NOT NULL AND NOT (lat = 0 AND lng = 0)
             THEN ST_SetSRID(ST_MakePoint(lng, lat), 4326) END
    ) STORED;

CREATE INDEX IF NOT EXISTS ov_geom_idx ON observation_versions USING gist (geom);
