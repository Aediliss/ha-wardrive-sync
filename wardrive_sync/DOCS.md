# Wardrive Sync

## Requirements

- **PostgreSQL + TimescaleDB** add-on (Expaso, `https://github.com/expaso/hassos-addons`), which includes PostGIS.
- **Cloudflared** add-on (`https://github.com/brenner-tobias/ha-addons`) to publish the endpoint without opening ports.

## Configuration

| Option | Meaning |
|---|---|
| `auth_token` | Bearer token the phones send. At least 32 chars (`openssl rand -hex 32`). |
| `db_host` / `db_port` | Postgres add-on hostname (shown on its Info page, e.g. `77b2833f-timescaledb`) and port. |
| `db_name` / `db_user` / `db_password` | Database and role this add-on uses. |
| `db_admin_user` / `db_admin_password` | **First start only.** If set, the add-on creates the role and database, enables PostGIS, then logs `bootstrap done`. Clear `db_admin_password` afterwards. |
| `archive_dir` | Where zips are kept (`pending/`, `archive/<label>/<YYYY-MM>/`, `quarantine/`). |
| `keep_archives_days` | Delete archived zips older than this. `0` keeps them forever. The database is not affected. |
| `max_upload_mb` | Upload cap (Cloudflare's free plan limit is 100 MB). |

## Publishing through Cloudflare

In the Cloudflared add-on, add an additional host:

```yaml
additional_hosts:
  - hostname: wd.example.com
    service: http://<this add-on's hostname>:8099
```

This add-on's hostname is shown on its Info page (`<repo-hash>-wardrive-sync`).

Recommended WAF custom rule (Block):

```
(http.host eq "wd.example.com") and (
  not (http.request.method eq "POST" and http.request.uri.path eq "/wardrive")
  or not any(lower(http.request.headers.names[*])[*] eq "authorization")
  or ip.src.country ne "US"
)
```

Also add a rate-limiting rule (e.g. 10 requests / 10 s per IP).

## Phones

Wardrive Go → Settings → Updates & maintenance → Backup / Restore data → **Sync to a URL**:
endpoint `https://wd.example.com/wardrive`, auth token = `auth_token`. Give each phone a
distinct name under **Boards › This phone**; that is the `label` data is stored under.

## Data

Schema `wardrive`. Main views: `latest_observations`, `observation_history`,
`surveillance_devices` (driven by the editable `watch_rules` table), `device_changes`,
`cell_tower_history`, and with PostGIS `surveillance_sites` / `surveillance_site_devices`
(devices clustered within 75 m, which reveals MAC rotation at one location).
See `queries.sql` for examples.
