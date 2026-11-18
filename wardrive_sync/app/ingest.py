"""Load a Wardrive Go backup zip into PostgreSQL as versioned history."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql

log = logging.getLogger("wardrive.ingest")
HERE = Path(__file__).resolve().parent

# ---- zip safety limits (the server only accepts zips from a token holder,
# but the content is still parsed, so cap everything) -----------------------
MAX_MEMBERS = 5000
MAX_MEMBER_BYTES = 1 << 30          # 1 GiB for any single file
MAX_TOTAL_BYTES = 2 << 30           # 2 GiB uncompressed in total
MAX_RATIO = 200                     # compression ratio cap (zip-bomb guard)
MAX_JSON_BYTES = 20 << 20           # parse sidecar JSON up to 20 MiB

NEVER_STORE = {"prefs.json"}        # holds WiGLE / WDGWars API keys
SKIP_TABLES = {"android_metadata", "sqlite_sequence", "room_master_table"}
DB_MEMBER = "wardrivego.db"


class BadUpload(Exception):
    """The upload itself is malformed; quarantine it instead of retrying."""


# ---------------------------------------------------------------- helpers

def _ts(ms):
    if ms is None:
        return None
    try:
        ms = float(ms)
        if ms <= 0:
            return None
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _bool(v):
    return None if v is None else bool(int(v))


def _int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _str(v):
    # Postgres text/jsonb cannot hold NUL; some BLE names contain them.
    return None if v is None else str(v).replace("\x00", "")


def _jsonable(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    if isinstance(v, str):
        return v.replace("\x00", "")
    return v


def _hash(obj) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(raw.encode()).hexdigest()


# Wardrive Go `observations` column -> (postgres column, converter)
OBS_MAP = {
    "key": ("key", _str), "type": ("type", _str), "name": ("name", _str),
    "maker": ("maker", _str), "category": ("category", _str), "icon": ("icon", _str),
    "best_rssi": ("best_rssi", _int), "last_rssi": ("last_rssi", _int),
    "times_seen": ("times_seen", _int), "first_seen": ("first_seen", _ts),
    "last_seen": ("last_seen", _ts), "company_id": ("company_id", _int),
    "channel": ("channel", _int), "frequency": ("frequency", _int),
    "capabilities": ("capabilities", _str), "connectable": ("connectable", _bool),
    "lat": ("lat", _float), "lng": ("lng", _float), "altitude": ("altitude", _float),
    "accuracy": ("accuracy", _float), "wifi_standard": ("wifi_standard", _int),
    "via_monitor": ("via_monitor", _bool), "notable": ("notable", _bool),
    "run_id": ("run_id", _int),
}
# Columns that change because of what the APP did (uploads, runs), not because
# the device changed. Kept in `extra`, excluded from the version hash.
BOOKKEEPING = {"in_wigle", "exported_at", "in_wdgw", "wdgw_sent_at", "run_id"}
OBS_PG_COLS = [pg for pg, _ in OBS_MAP.values()]


# ---------------------------------------------------------------- schema

def connect(cfg) -> psycopg.Connection:
    return psycopg.connect(
        host=cfg["db_host"], port=cfg["db_port"], dbname=cfg["db_name"],
        user=cfg["db_user"], password=cfg["db_password"],
        connect_timeout=10, application_name="wardrive-sync",
        autocommit=True,  # each `with conn.transaction()` is then a real commit
    )


def bootstrap(cfg) -> bool:
    """One-time setup with admin credentials, if provided: create the app role
    (or reset its password to match the config), the database, and PostGIS.
    Idempotent. Clear db_admin_password from the add-on config afterwards."""
    admin_pw = cfg.get("db_admin_password") or ""
    if not admin_pw:
        return False
    base = dict(host=cfg["db_host"], port=cfg["db_port"], user=cfg.get("db_admin_user") or "postgres",
                password=admin_pw, connect_timeout=10, autocommit=True,
                application_name="wardrive-sync-bootstrap")
    role, db = cfg["db_user"], cfg["db_name"]
    with psycopg.connect(dbname="postgres", **base) as c:
        exists = c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        stmt = "ALTER ROLE {} WITH LOGIN PASSWORD {}" if exists else "CREATE ROLE {} LOGIN PASSWORD {}"
        c.execute(sql.SQL(stmt).format(sql.Identifier(role), sql.Literal(cfg["db_password"])))
        if not c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,)).fetchone():
            c.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(db), sql.Identifier(role)))
            log.info("created database %s", db)
    with psycopg.connect(dbname=db, **base) as c:
        try:
            c.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        except psycopg.Error as e:
            log.warning("could not enable PostGIS (location views stay off): %s", e)
    log.info("bootstrap done for role %s; remove db_admin_password from the config now", role)
    return True


def ensure_schema(conn) -> bool:
    """Create/upgrade tables and views. Returns True if PostGIS is active."""
    with conn.transaction():
        conn.execute((HERE / "schema.sql").read_text())
        has_postgis = conn.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'postgis'").fetchone() is not None
        if has_postgis:
            conn.execute((HERE / "postgis.sql").read_text())
        conn.execute((HERE / "views.sql").read_text())
        if has_postgis:
            conn.execute((HERE / "postgis_views.sql").read_text())
    return has_postgis


def is_known(conn, sha256: str) -> bool:
    return conn.execute("SELECT 1 FROM wardrive.uploads WHERE sha256 = %s",
                        (sha256,)).fetchone() is not None


# ---------------------------------------------------------------- zip

def check_zip(zf: zipfile.ZipFile) -> None:
    infos = zf.infolist()
    if not infos:
        raise BadUpload("empty zip")
    if len(infos) > MAX_MEMBERS:
        raise BadUpload(f"too many members ({len(infos)})")
    total = 0
    for i in infos:
        if i.flag_bits & 0x1:
            raise BadUpload(f"encrypted member {i.filename!r}")
        if i.file_size > MAX_MEMBER_BYTES:
            raise BadUpload(f"member too large {i.filename!r}")
        if i.file_size > (50 << 20) and i.file_size > MAX_RATIO * max(i.compress_size, 1):
            raise BadUpload(f"suspicious compression ratio {i.filename!r}")
        total += i.file_size
    if total > MAX_TOTAL_BYTES:
        raise BadUpload("uncompressed total too large")


def repack_without_secrets(src: Path, dst: Path) -> None:
    """Copy the zip, dropping prefs.json. Atomic via temp file + rename."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            if Path(info.filename).name in NEVER_STORE or info.is_dir():
                continue
            with zin.open(info) as fin, zout.open(info.filename, "w", force_zip64=True) as fout:
                shutil.copyfileobj(fin, fout, 1 << 20)
    tmp.replace(dst)


def _read_member(zf, info, limit):
    with zf.open(info) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise BadUpload(f"member exceeds limit {info.filename!r}")
    return data


# ---------------------------------------------------------------- loaders

def _load_observations(conn, scon, upload_id, label) -> dict:
    cols = [r[1] for r in scon.execute('PRAGMA table_info("observations")')]
    mapped = [c for c in cols if c in OBS_MAP]
    unknown = [c for c in cols if c not in OBS_MAP]
    sel = ", ".join(f'"{c}"' for c in cols)

    pg_cols = ["label", "row_hash", "first_upload_id", *OBS_PG_COLS, "extra"]
    seen = skipped = 0
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE tmp_obs (LIKE wardrive.observation_versions "
                    "INCLUDING DEFAULTS) ON COMMIT DROP")
        with cur.copy(f"COPY tmp_obs ({', '.join(pg_cols)}) FROM STDIN") as cp:
            for row in scon.execute(f'SELECT {sel} FROM "observations"'):
                rec = dict(zip(cols, row))
                if rec.get("key") is None or rec.get("type") is None:
                    skipped += 1
                    continue
                vals = {OBS_MAP[c][0]: OBS_MAP[c][1](rec[c]) for c in mapped}
                extra = {c: _jsonable(rec[c]) for c in unknown}
                hashed = {c: _jsonable(rec[c]) for c in cols if c not in BOOKKEEPING}
                cp.write_row((label, _hash(hashed), upload_id,
                              *[vals.get(c) for c in OBS_PG_COLS],
                              json.dumps(extra, default=str) if extra else None))
                seen += 1
        cur.execute(f"""
            INSERT INTO wardrive.observation_versions ({', '.join(pg_cols)})
            SELECT {', '.join(pg_cols)} FROM tmp_obs
            ON CONFLICT (label, key, row_hash) DO NOTHING""")
        new_versions = cur.rowcount
    return {"observations_rows": seen, "observations_new_versions": new_versions,
            "observations_skipped": skipped}


def _load_generic(conn, scon, table, upload_id, label) -> int:
    info = list(scon.execute(f'PRAGMA table_info("{table}")'))
    cols = [r[1] for r in info]
    pk_cols = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5] > 0]
    sel = ", ".join(f'"{c}"' for c in cols)
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE IF NOT EXISTS tmp_snap "
                    "(LIKE wardrive.snapshot_rows INCLUDING DEFAULTS) ON COMMIT DROP")
        cur.execute("TRUNCATE tmp_snap")
        with cur.copy("COPY tmp_snap (table_name, label, pk, row_hash, first_upload_id, data) "
                      "FROM STDIN") as cp:
            for row in scon.execute(f'SELECT {sel} FROM "{table}"'):
                data = {c: _jsonable(v) for c, v in zip(cols, row)}
                h = _hash(data)
                pk = json.dumps([data[c] for c in pk_cols], default=str) if pk_cols else h
                cp.write_row((table, label, pk, h, upload_id, json.dumps(data, default=str)))
        cur.execute("""
            INSERT INTO wardrive.snapshot_rows (table_name, label, pk, row_hash, first_upload_id, data)
            SELECT table_name, label, pk, row_hash, first_upload_id, data FROM tmp_snap
            ON CONFLICT (table_name, label, pk, row_hash) DO NOTHING""")
        return cur.rowcount


def _load_sqlite(conn, db_path: Path, upload_id, label) -> dict:
    uri = f"file:{db_path}?mode=ro&immutable=1"
    scon = sqlite3.connect(uri, uri=True)
    try:
        try:
            tables = [r[0] for r in scon.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")]
        except sqlite3.DatabaseError as e:
            raise BadUpload(f"{DB_MEMBER} is not a valid SQLite database: {e}") from e
        stats = {}
        for t in tables:
            if t in SKIP_TABLES or t.startswith("sqlite_"):
                continue
            if t == "observations":
                stats.update(_load_observations(conn, scon, upload_id, label))
            else:
                stats[f"{t}_new_versions"] = _load_generic(conn, scon, t, upload_id, label)
        return stats
    finally:
        scon.close()


def _load_sidecars(conn, zf, upload_id, label) -> int:
    added = 0
    for info in zf.infolist():
        name = info.filename
        if info.is_dir() or name == DB_MEMBER or Path(name).name in NEVER_STORE:
            continue
        h = hashlib.sha256()
        parsed = None
        with zf.open(info) as f:
            if name.lower().endswith(".json") and info.file_size <= MAX_JSON_BYTES:
                raw = f.read(MAX_JSON_BYTES + 1)
                h.update(raw)
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = None
            else:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
        cur = conn.execute(
            """INSERT INTO wardrive.sidecar_files (label, name, sha256, bytes, first_upload_id, data)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (label, name, sha256) DO NOTHING""",
            (label, name, h.hexdigest(), info.file_size, upload_id,
             json.dumps(parsed) if parsed is not None else None))
        added += cur.rowcount
    return added


# ---------------------------------------------------------------- entry point

def ingest_upload(conn, zip_path: Path, meta: dict, archive_path: str | None) -> dict:
    """Load one upload in a single transaction. Returns stats."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise BadUpload(f"not a zip: {e}") from e
    with zf, tempfile.TemporaryDirectory(prefix="wardrive-") as td:
        check_zip(zf)
        names = set(zf.namelist())
        manifest = None
        if "manifest.json" in names:
            try:
                manifest = json.loads(_read_member(zf, zf.getinfo("manifest.json"), 1 << 20))
            except ValueError:
                manifest = None
        db_path = None
        if DB_MEMBER in names:
            db_path = Path(td) / "w.db"
            with zf.open(DB_MEMBER) as fin, open(db_path, "wb") as fout:
                shutil.copyfileobj(fin, fout, 1 << 20)
        elif manifest is None:
            raise BadUpload("zip has neither wardrivego.db nor manifest.json")

        label = meta["label"]
        with conn.transaction():
            row = conn.execute(
                """INSERT INTO wardrive.uploads
                   (sha256, received_at, label, device, app_version, schema_version,
                    hdr_networks, hdr_captures, bytes, remote_ip, manifest, archive_path)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (sha256) DO NOTHING RETURNING id""",
                (meta["sha256"], meta["received_at"], label, meta.get("device"),
                 meta.get("app"), _int(meta.get("schema")), _int(meta.get("networks")),
                 _int(meta.get("captures")), meta.get("bytes"), meta.get("remote_ip"),
                 json.dumps(manifest) if manifest is not None else None, archive_path),
            ).fetchone()
            if row is None:
                return {"status": "duplicate"}
            upload_id = row[0]
            stats = {}
            if db_path is not None:
                stats.update(_load_sqlite(conn, db_path, upload_id, label))
            stats["sidecar_new_files"] = _load_sidecars(conn, zf, upload_id, label)
            conn.execute("UPDATE wardrive.uploads SET stats = %s WHERE id = %s",
                         (json.dumps(stats), upload_id))
    return {"status": "ingested", "upload_id": upload_id, **stats}
