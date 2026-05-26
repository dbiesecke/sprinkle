#!/usr/bin/env python3
"""
Service account registry and quota cache.
"""

import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import stat


DEFAULT_DB_PATH = os.path.expanduser("~/.sprinkle/sa-cache.sqlite3")
DEFAULT_STORE_DIR = os.path.expanduser("~/.sprinkle/service-accounts")
DEFAULT_CACHE_TTL_HOURS = 72
DEFAULT_CLEAN_INVALID = "quarantine"
DEFAULT_REFRESH_MODE = "stale"

REQUIRED_FIELDS = [
    "type",
    "project_id",
    "private_key_id",
    "private_key",
    "client_email",
    "client_id",
    "token_uri",
]


class ImportResult(object):
    def __init__(self):
        self.total = 0
        self.scanned = 0
        self.validated = 0
        self.imported = 0
        self.duplicates = 0
        self.invalid = 0
        self.validation_errors = 0
        self.quarantined = 0
        self.deleted = 0
        self.selected_files = []


class ServiceAccountRegistry(object):
    def __init__(self, db_path=None, store_dir=None, cache_ttl_hours=DEFAULT_CACHE_TTL_HOURS):
        self.db_path = os.path.abspath(os.path.expanduser(db_path or DEFAULT_DB_PATH))
        self.store_dir = os.path.abspath(os.path.expanduser(store_dir or DEFAULT_STORE_DIR))
        self.quarantine_dir = os.path.join(self.store_dir, "quarantine")
        self.cache_ttl_hours = int(cache_ttl_hours)
        self._ensure_dirs()
        self._init_db()

    def _ensure_dirs(self):
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            if not os.path.isdir(db_dir):
                os.makedirs(db_dir, mode=0o700, exist_ok=True)
                os.chmod(db_dir, 0o700)
        os.makedirs(self.store_dir, mode=0o700, exist_ok=True)
        os.makedirs(self.quarantine_dir, mode=0o700, exist_ok=True)
        os.chmod(self.store_dir, 0o700)
        os.chmod(self.quarantine_dir, 0o700)

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_key TEXT,
                    client_email TEXT,
                    private_key_id TEXT,
                    project_id TEXT,
                    client_id TEXT,
                    content_hash TEXT NOT NULL,
                    source_path TEXT,
                    managed_path TEXT,
                    remote_name TEXT,
                    status TEXT NOT NULL,
                    invalid_reason TEXT,
                    duplicate_of INTEGER,
                    imported_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS quota_cache (
                    account_id INTEGER PRIMARY KEY,
                    total INTEGER,
                    used INTEGER,
                    free INTEGER,
                    trashed INTEGER,
                    other INTEGER,
                    objects INTEGER,
                    last_about_at TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_email ON accounts(client_email)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_key_id ON accounts(private_key_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_hash ON accounts(content_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_source ON accounts(source_path)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_remote ON accounts(remote_name)")

    def import_paths(self, paths, clean_invalid=DEFAULT_CLEAN_INVALID, validator=None, progress=None):
        if clean_invalid not in ("none", "quarantine", "delete"):
            raise ValueError("invalid service account cleanup mode: {}".format(clean_invalid))
        result = ImportResult()
        json_paths = []
        for path in paths:
            for json_path in self._iter_json_files(path):
                json_paths.append(json_path)
        result.total = len(json_paths)
        self._emit_progress(progress, {
            "event": "start",
            "total": result.total,
        })
        for index, json_path in enumerate(json_paths, 1):
            self._emit_progress(progress, {
                "event": "file",
                "index": index,
                "total": result.total,
                "path": json_path,
            })
            try:
                result.scanned += 1
                self._import_file(json_path, clean_invalid, result, validator, progress, index)
            except Exception as exc:
                reason = "import error: {}".format(exc)
                self._record_invalid(json_path, b"", {}, None, reason, clean_invalid, result, self._utcnow())
                self._emit_progress(progress, {
                    "event": "status",
                    "index": index,
                    "total": result.total,
                    "path": json_path,
                    "status": "invalid",
                    "reason": reason,
                })
        result.selected_files = sorted(set(result.selected_files))
        self._emit_progress(progress, {
            "event": "complete",
            "total": result.total,
            "result": result,
        })
        return result

    def _iter_json_files(self, path):
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isfile(path):
            if path.endswith(".json"):
                yield path
            return
        if not os.path.isdir(path):
            raise ValueError("service account path not found: {}".format(path))
        for root, _, files in os.walk(path):
            for filename in files:
                if filename.endswith(".json"):
                    yield os.path.join(root, filename)

    def _import_file(self, path, clean_invalid, result, validator=None, progress=None, index=None):
        with open(path, "rb") as fp:
            raw = fp.read()
        content_hash = hashlib.sha256(raw).hexdigest()
        now = self._utcnow()
        try:
            payload = json.loads(raw.decode("utf-8"))
            invalid_reason = self.validate_payload(payload)
        except Exception as exc:
            payload = {}
            invalid_reason = "invalid json: {}".format(exc.__class__.__name__)

        if invalid_reason is not None:
            self._record_invalid(path, raw, payload, content_hash, invalid_reason, clean_invalid, result, now)
            self._emit_status(progress, index, result.total, path, "invalid", invalid_reason)
            return

        duplicate = self._find_duplicate(payload, content_hash)
        account_key = self._account_key(payload, content_hash)
        if duplicate is not None:
            result.duplicates += 1
            self._record_account(
                account_key=account_key,
                payload=payload,
                content_hash=content_hash,
                source_path=path,
                managed_path=duplicate["managed_path"],
                status="duplicate",
                invalid_reason=None,
                duplicate_of=duplicate["id"],
                now=now,
            )
            if duplicate["managed_path"]:
                result.selected_files.append(duplicate["managed_path"])
            self._emit_status(progress, index, result.total, path, "duplicate", None)
            return

        quota = None
        if validator is not None:
            try:
                quota, validation_error = validator(path, payload)
            except Exception as exc:
                quota = None
                validation_error = "validation error: {}".format(exc)
            if validation_error is not None:
                result.validation_errors += 1
                self._record_invalid(
                    path,
                    raw,
                    payload,
                    content_hash,
                    validation_error,
                    clean_invalid,
                    result,
                    now,
                    account_key,
                )
                self._emit_status(progress, index, result.total, path, "invalid", validation_error)
                return
            result.validated += 1

        managed_path = self._managed_path(account_key)
        shutil.copyfile(path, managed_path)
        os.chmod(managed_path, stat.S_IRUSR | stat.S_IWUSR)
        account_id = self._record_account(
            account_key=account_key,
            payload=payload,
            content_hash=content_hash,
            source_path=path,
            managed_path=managed_path,
            status="active",
            invalid_reason=None,
            duplicate_of=None,
            now=now,
        )
        if quota is not None:
            self.update_quota(account_id, quota, None)
        result.imported += 1
        result.selected_files.append(managed_path)
        self._emit_status(progress, index, result.total, path, "imported", None)
        return account_id

    def _record_invalid(
            self,
            path,
            raw,
            payload,
            content_hash,
            invalid_reason,
            clean_invalid,
            result,
            now,
            account_key=None):
        if content_hash is None:
            content_hash = hashlib.sha256(raw).hexdigest()
        result.invalid += 1
        managed_path = None
        if clean_invalid == "quarantine":
            managed_path = self._quarantine(path, content_hash, raw)
            result.quarantined += 1
        elif clean_invalid == "delete":
            os.remove(path)
            result.deleted += 1
        self._record_account(
            account_key=account_key,
            payload=payload,
            content_hash=content_hash,
            source_path=path,
            managed_path=managed_path,
            status="invalid",
            invalid_reason=invalid_reason,
            duplicate_of=None,
            now=now,
        )

    def _emit_status(self, progress, index, total, path, status, reason):
        self._emit_progress(progress, {
            "event": "status",
            "index": index,
            "total": total,
            "path": path,
            "status": status,
            "reason": reason,
        })

    def _emit_progress(self, progress, event):
        if progress is not None:
            progress(event)

    def validate_payload(self, payload):
        if not isinstance(payload, dict):
            return "json root is not an object"
        missing = [field for field in REQUIRED_FIELDS if not payload.get(field)]
        if missing:
            return "missing required fields: {}".format(",".join(missing))
        if payload.get("type") != "service_account":
            return "type is not service_account"
        private_key = payload.get("private_key", "")
        if "BEGIN PRIVATE KEY" not in private_key:
            return "private_key is not a private key"
        return None

    def _find_duplicate(self, payload, content_hash):
        with self._connect() as conn:
            client_email = payload.get("client_email")
            if client_email:
                row = conn.execute(
                    "SELECT * FROM accounts WHERE status='active' AND client_email=? ORDER BY id LIMIT 1",
                    (client_email,),
                ).fetchone()
                if row is not None:
                    return row
            private_key_id = payload.get("private_key_id")
            if private_key_id:
                row = conn.execute(
                    "SELECT * FROM accounts WHERE status='active' AND private_key_id=? ORDER BY id LIMIT 1",
                    (private_key_id,),
                ).fetchone()
                if row is not None:
                    return row
            row = conn.execute(
                "SELECT * FROM accounts WHERE status='active' AND content_hash=? ORDER BY id LIMIT 1",
                (content_hash,),
            ).fetchone()
            return row

    def _record_account(
            self,
            account_key,
            payload,
            content_hash,
            source_path,
            managed_path,
            status,
            invalid_reason,
            duplicate_of,
            now):
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO accounts (
                    account_key, client_email, private_key_id, project_id, client_id,
                    content_hash, source_path, managed_path, status, invalid_reason,
                    duplicate_of, imported_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_key,
                    payload.get("client_email"),
                    payload.get("private_key_id"),
                    payload.get("project_id"),
                    payload.get("client_id"),
                    content_hash,
                    source_path,
                    managed_path,
                    status,
                    invalid_reason,
                    duplicate_of,
                    now,
                    now,
                ),
            )
            return cursor.lastrowid

    def _account_key(self, payload, content_hash):
        if payload.get("client_email"):
            return "email:" + payload["client_email"]
        if payload.get("private_key_id"):
            return "key:" + payload["private_key_id"]
        return "hash:" + content_hash

    def _managed_path(self, account_key):
        digest = hashlib.sha256(account_key.encode("utf-8")).hexdigest()
        return os.path.join(self.store_dir, "sa-{}.json".format(digest[:24]))

    def _quarantine(self, source_path, content_hash, raw):
        filename = "invalid-{}-{}".format(content_hash[:24], os.path.basename(source_path))
        path = os.path.join(self.quarantine_dir, filename)
        with open(path, "wb") as fp:
            fp.write(raw)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        return path

    def active_accounts(self, limit=None):
        sql = "SELECT * FROM accounts WHERE status='active' ORDER BY client_email, id"
        params = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        with self._connect() as conn:
            return conn.execute(sql, params).fetchall()

    def all_account_stats(self):
        with self._connect() as conn:
            return conn.execute("""
                SELECT
                    a.*,
                    q.total, q.used, q.free, q.trashed, q.other, q.objects,
                    q.last_about_at, q.last_error
                FROM accounts a
                LEFT JOIN quota_cache q ON q.account_id = a.id
                ORDER BY a.status, a.client_email, a.id
            """).fetchall()

    def summary_counts(self):
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM accounts GROUP BY status").fetchall()
        return dict((row["status"], row["count"]) for row in rows)

    def assign_remote_names(self, entries):
        now = self._utcnow()
        with self._connect() as conn:
            for entry in entries:
                managed_path = os.path.abspath(entry["path"])
                remote = entry["remote"].rstrip(":")
                conn.execute(
                    """
                    UPDATE accounts
                    SET remote_name=?, updated_at=?
                    WHERE status='active' AND managed_path=?
                    """,
                    (remote, now, managed_path),
                )

    def quota_by_remote(self, remote):
        remote_name = remote.rstrip(":")
        with self._connect() as conn:
            return conn.execute("""
                SELECT
                    a.id AS account_id,
                    a.remote_name,
                    q.total, q.used, q.free, q.trashed, q.other, q.objects,
                    q.last_about_at, q.last_error
                FROM accounts a
                LEFT JOIN quota_cache q ON q.account_id = a.id
                WHERE a.status='active' AND a.remote_name=?
                ORDER BY a.id
                LIMIT 1
            """, (remote_name,)).fetchone()

    def quota_by_account_id(self, account_id):
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM quota_cache WHERE account_id=?",
                (account_id,),
            ).fetchone()

    def should_refresh(self, quota_row, mode):
        if mode == "none":
            return False
        if mode == "all":
            return True
        if quota_row is None or quota_row["last_about_at"] is None:
            return mode in ("missing", "stale")
        if mode == "missing":
            return False
        if mode == "stale":
            return self.is_stale(quota_row["last_about_at"])
        return False

    def is_stale(self, last_about_at):
        if last_about_at is None:
            return True
        last = datetime.datetime.strptime(last_about_at, "%Y-%m-%dT%H:%M:%SZ")
        age = datetime.datetime.utcnow() - last
        return age.total_seconds() > self.cache_ttl_hours * 3600

    def update_quota_for_remote(self, remote, quota, error=None):
        row = self.quota_by_remote(remote)
        if row is None:
            return
        self.update_quota(row["account_id"], quota, error)

    def update_quota(self, account_id, quota, error=None):
        now = self._utcnow()
        if error is not None:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT account_id FROM quota_cache WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                if existing is None:
                    conn.execute("""
                        INSERT INTO quota_cache (
                            account_id, last_about_at, last_error, updated_at
                        ) VALUES (?, ?, ?, ?)
                    """, (account_id, None, error, now))
                else:
                    conn.execute("""
                        UPDATE quota_cache
                        SET last_error=?, updated_at=?
                        WHERE account_id=?
                    """, (error, now, account_id))
            return
        quota = quota or {}
        last_about_at = now if error is None else None
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO quota_cache (
                    account_id, total, used, free, trashed, other, objects,
                    last_about_at, last_error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    total=excluded.total,
                    used=excluded.used,
                    free=excluded.free,
                    trashed=excluded.trashed,
                    other=excluded.other,
                    objects=excluded.objects,
                    last_about_at=excluded.last_about_at,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
            """, (
                account_id,
                quota.get("total"),
                quota.get("used"),
                quota.get("free"),
                quota.get("trashed"),
                quota.get("other"),
                quota.get("objects"),
                last_about_at,
                error,
                now,
            ))

    def adjust_quota_for_remote(self, remote, byte_delta):
        row = self.quota_by_remote(remote)
        if row is None:
            return
        now = self._utcnow()
        free = row["free"]
        used = row["used"]
        if free is not None:
            free = max(0, free - int(byte_delta))
        if used is not None:
            used = used + int(byte_delta)
        with self._connect() as conn:
            conn.execute("""
                UPDATE quota_cache
                SET free=?, used=?, updated_at=?
                WHERE account_id=?
            """, (free, used, now, row["account_id"]))

    @staticmethod
    def _utcnow():
        return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
