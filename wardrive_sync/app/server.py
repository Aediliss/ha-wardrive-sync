#!/usr/bin/env python3
"""Wardrive Go "Sync to a URL" receiver + ingest worker.

POST /wardrive  (Authorization: Bearer <token>, Content-Type: application/zip)
  -> the zip is streamed to <archive_dir>/pending/ and 202 is returned at once.
     Receiving never depends on the database being up.
A background worker loads pending zips into PostgreSQL, moves them to
<archive_dir>/archive/<label>/<YYYY-MM>/ (with prefs.json removed), and
quarantines anything malformed.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg

import ingest

OPTIONS_FILE = os.environ.get("WARDRIVE_OPTIONS", "/data/options.json")
LISTEN_HOST = os.environ.get("WARDRIVE_LISTEN", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("WARDRIVE_PORT", "8099"))
ENDPOINT = "/wardrive"
ACCEPTED_TYPES = {"application/zip", "application/x-zip-compressed", "application/octet-stream"}
LABEL_RE = re.compile(r"[^A-Za-z0-9._-]+")
FAIL_LIMIT, FAIL_WINDOW = 10, 600          # 10 bad tokens per IP per 10 min -> 429

log = logging.getLogger("wardrive")


def load_options() -> dict:
    with open(OPTIONS_FILE) as f:
        cfg = json.load(f)
    if len(cfg.get("auth_token") or "") < 32:
        raise SystemExit("auth_token must be set and at least 32 characters. "
                         "Generate one with: openssl rand -hex 32")
    if not cfg.get("db_password"):
        raise SystemExit("db_password must be set")
    cfg.setdefault("archive_dir", "/share/wardrive")
    cfg.setdefault("keep_archives_days", 0)
    cfg.setdefault("max_upload_mb", 95)
    cfg.setdefault("log_level", "info")
    return cfg


def safe_label(raw: str | None) -> str:
    s = LABEL_RE.sub("_", (raw or "").strip())[:64].strip("._")
    return s or "unknown"


class FailTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._fails: dict[str, collections.deque] = collections.defaultdict(collections.deque)

    def _trim(self, q, now):
        while q and now - q[0] > FAIL_WINDOW:
            q.popleft()

    def blocked(self, ip: str) -> bool:
        with self._lock:
            q = self._fails[ip]
            self._trim(q, time.monotonic())
            return len(q) >= FAIL_LIMIT

    def add(self, ip: str) -> None:
        with self._lock:
            q = self._fails[ip]
            now = time.monotonic()
            self._trim(q, now)
            q.append(now)


class TooLarge(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    server_version = "wardrive-sync"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing -----------------------------------------------------------
    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_ip(), fmt % args)

    def client_ip(self) -> str:
        return self.headers.get("CF-Connecting-IP") or self.client_address[0]

    def reply(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body_chunks(self, limit: int):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            total = 0
            while True:
                line = self.rfile.readline(1024)
                size = int(line.split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                        pass
                    return
                total += size
                if total > limit:
                    raise TooLarge
                remaining = size
                while remaining:
                    chunk = self.rfile.read(min(remaining, 1 << 20))
                    if not chunk:
                        raise ConnectionError("client closed mid-chunk")
                    remaining -= len(chunk)
                    yield chunk
                self.rfile.readline(16)
        else:
            length = self.headers.get("Content-Length")
            if length is None:
                raise ValueError("missing Content-Length")
            remaining = int(length)
            if remaining <= 0:
                raise ValueError("empty body")
            if remaining > limit:
                raise TooLarge
            while remaining:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    raise ConnectionError("client closed early")
                remaining -= len(chunk)
                yield chunk

    # -- routes -------------------------------------------------------------
    def do_GET(self):
        if self.path == "/healthz":
            self.reply(200, {"ok": True})
        else:
            self.reply(404, {"error": "not found"})

    do_PUT = do_DELETE = do_PATCH = do_HEAD = lambda self: self.reply(405, {"error": "method not allowed"})

    def do_POST(self):
        cfg = self.server.cfg
        ip = self.client_ip()
        if self.path.split("?", 1)[0].rstrip("/") != ENDPOINT:
            return self.reply(404, {"error": "not found"})
        if self.server.fails.blocked(ip):
            log.warning("rate-limited %s", ip)
            return self.reply(429, {"error": "too many failed attempts"})

        auth = self.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if not hmac.compare_digest(token.encode(), cfg["auth_token"].encode()):
            self.server.fails.add(ip)
            log.warning("rejected bad/missing token from %s", ip)
            return self.reply(401, {"error": "unauthorized"})

        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype not in ACCEPTED_TYPES:
            return self.reply(415, {"error": "expected application/zip"})

        label = safe_label(self.headers.get("X-Wardrive-Label"))
        received = datetime.now(timezone.utc)
        pending = Path(cfg["archive_dir"]) / "pending"
        pending.mkdir(parents=True, exist_ok=True)
        stem = f"{received:%Y%m%dT%H%M%S%fZ}_{label}"
        part = pending / f"{stem}.zip.part"
        sha = hashlib.sha256()
        size = 0
        try:
            with open(part, "wb") as f:
                for chunk in self._body_chunks(cfg["max_upload_mb"] << 20):
                    sha.update(chunk)
                    size += len(chunk)
                    f.write(chunk)
        except TooLarge:
            part.unlink(missing_ok=True)
            return self.reply(413, {"error": "upload too large"})
        except (ValueError, ConnectionError) as e:
            part.unlink(missing_ok=True)
            log.warning("bad request body from %s: %s", ip, e)
            return self.reply(400, {"error": "bad body"})

        if not zipfile.is_zipfile(part):
            part.unlink(missing_ok=True)
            return self.reply(422, {"error": "body is not a zip"})

        digest = sha.hexdigest()
        meta = {
            "sha256": digest, "received_at": received.isoformat(), "label": label,
            "device": (self.headers.get("X-Wardrive-Device") or "")[:128] or None,
            "app": (self.headers.get("X-Wardrive-App") or "")[:32] or None,
            "schema": self.headers.get("X-Wardrive-Schema"),
            "networks": self.headers.get("X-Wardrive-Networks"),
            "captures": self.headers.get("X-Wardrive-Captures"),
            "bytes": size, "remote_ip": ip,
        }
        final = pending / f"{stem}.zip"
        part.replace(final)
        (pending / f"{stem}.json").write_text(json.dumps(meta))
        log.info("received %s from %s (%s, %.1f MB, sha %s)", label, ip,
                 meta["device"], size / 1e6, digest[:12])
        self.server.wake.set()
        self.reply(202, {"status": "queued", "sha256": digest, "label": label})


# ---------------------------------------------------------------- worker

class Worker(threading.Thread):
    def __init__(self, cfg, wake):
        super().__init__(daemon=True, name="ingest")
        self.cfg, self.wake = cfg, wake
        self.root = Path(cfg["archive_dir"])
        self.conn = None
        self.schema_ready = False
        self.bootstrapped = False

    def _conn(self):
        if not self.bootstrapped:
            ingest.bootstrap(self.cfg)
            self.bootstrapped = True
        if self.conn is None or self.conn.closed:
            self.conn = ingest.connect(self.cfg)
            self.schema_ready = False
        if not self.schema_ready:
            postgis = ingest.ensure_schema(self.conn)
            self.schema_ready = True
            log.info("database schema ready (PostGIS %s)", "on" if postgis else "off")
        return self.conn

    def run(self):
        backoff = 5
        while True:
            try:
                self._conn()                           # surface DB/schema problems at startup
                self.process_pending()
                self.prune()
                backoff = 5
                self.wake.wait(60)
            except psycopg.OperationalError as e:
                log.warning("database unavailable (%s); retrying in %ss", e, backoff)
                self.conn = None
                self.wake.wait(backoff)
                backoff = min(backoff * 2, 300)
            except Exception:
                log.exception("worker error")
                self.wake.wait(30)
            self.wake.clear()

    def process_pending(self):
        pending = self.root / "pending"
        if not pending.is_dir():
            return
        for meta_file in sorted(pending.glob("*.json")):
            zip_file = meta_file.with_suffix(".zip")
            if not zip_file.exists():
                meta_file.unlink(missing_ok=True)
                continue
            meta = json.loads(meta_file.read_text())
            conn = self._conn()
            if ingest.is_known(conn, meta["sha256"]):
                log.info("duplicate upload %s (%s), discarding", meta["sha256"][:12], meta["label"])
                zip_file.unlink(missing_ok=True)
                meta_file.unlink(missing_ok=True)
                continue
            month = meta["received_at"][:7]
            dest = self.root / "archive" / meta["label"] / month / zip_file.name
            started = time.monotonic()
            try:
                ingest.repack_without_secrets(zip_file, dest)
                result = ingest.ingest_upload(conn, zip_file, meta, str(dest))
            except (ingest.BadUpload, zipfile.BadZipFile) as e:
                self._quarantine(zip_file, meta_file, dest, e)
                continue
            except psycopg.OperationalError:
                raise                                   # DB down: retry the whole queue later
            except (psycopg.Error, ValueError) as e:
                # Data the DB rejected. Retry a few times, then set it aside so
                # one bad upload can't block the queue.
                meta["attempts"] = int(meta.get("attempts", 0)) + 1
                meta_file.write_text(json.dumps(meta))
                dest.unlink(missing_ok=True)
                if meta["attempts"] >= 5:
                    self._quarantine(zip_file, meta_file, dest, e)
                else:
                    log.error("ingest failed for %s (attempt %s): %s",
                              zip_file.name, meta["attempts"], e)
                continue
            zip_file.unlink(missing_ok=True)
            meta_file.unlink(missing_ok=True)
            log.info("ingested %s in %.1fs: %s", zip_file.name,
                     time.monotonic() - started, json.dumps(result))

    def _quarantine(self, zip_file, meta_file, dest, err):
        q = self.root / "quarantine"
        q.mkdir(parents=True, exist_ok=True)
        zip_file.replace(q / zip_file.name)
        meta_file.replace(q / meta_file.name)
        dest.unlink(missing_ok=True)
        log.error("quarantined %s: %s", zip_file.name, err)

    def prune(self):
        days = int(self.cfg.get("keep_archives_days") or 0)
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        for p in (self.root / "archive").rglob("*.zip"):
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                log.info("pruned old archive %s", p)


def main():
    cfg = load_options()
    logging.basicConfig(
        level=getattr(logging, str(cfg["log_level"]).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Path(cfg["archive_dir"]).mkdir(parents=True, exist_ok=True)
    os.chmod(cfg["archive_dir"], 0o700)

    wake = threading.Event()
    Worker(cfg, wake).start()

    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    httpd.cfg, httpd.wake, httpd.fails = cfg, wake, FailTracker()
    httpd.daemon_threads = True
    log.info("listening on %s:%s%s", LISTEN_HOST, LISTEN_PORT, ENDPOINT)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
