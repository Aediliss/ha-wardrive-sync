# Wardrive Sync (Home Assistant add-on)

Receives [Wardrive Go](https://github.com/RocketGod-git/wardrive-go) **Sync to a URL** backups
and loads them into PostgreSQL (+ PostGIS) as a change history, so you can see how devices,
including surveillance gear such as Flock cameras, change over time (renames, MAC rotation, moves).

- `POST /wardrive` with `Authorization: Bearer <token>`, `Content-Type: application/zip`
- Upload is written to disk and acknowledged immediately; a worker ingests it afterwards
- Each sync is deduplicated; a new row version is stored only when a device actually changed
- `prefs.json` (holds WiGLE / WDGWars API keys) is never stored
- No host ports: expose it through the Cloudflared add-on, with Cloudflare WAF rules in front

See [wardrive_sync/DOCS.md](wardrive_sync/DOCS.md) for setup.

## Install

Settings → Add-ons → Add-on Store → ⋮ → Repositories → add
`https://github.com/Aediliss/ha-wardrive-sync`, then install **Wardrive Sync**.
