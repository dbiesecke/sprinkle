#!/usr/bin/env python3
"""
Service account registry and quota cache.
"""

import datetime
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager


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

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ls_cache (
                    account_id INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    json_text TEXT NOT NULL,
                    object_count INTEGER,
                    dir_count INTEGER,
                    file_count INTEGER,
                    last_lsjson_at TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, path),
                    FOREIGN KEY(account_id) REFERENCES accounts(id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_email ON accounts(client_email)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_key_id ON accounts(private_key_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_hash ON accounts(content_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_source ON accounts(source_path)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_remote ON accounts(remote_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ls_cache_path ON ls_cache(path)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rc_slots (
                    remote_name TEXT PRIMARY KEY,
                    account_id INTEGER,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id)
                )
            """)
            # Union runs deliberately retain account ids only.  Credentials stay
            # in the managed store and are never copied into run metadata.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS union_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_key TEXT NOT NULL,
                    target_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_key, target_key)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS union_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    batch_index INTEGER NOT NULL,
                    account_ids TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    last_error TEXT,
                    FOREIGN KEY(run_id) REFERENCES union_runs(id),
                    UNIQUE(run_id, batch_index)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_union_batches_run ON union_batches(run_id)")

    def union_run(self, source_key, target_key):
        """Return the durable run identity for one canonical source/target."""
        now = self._utcnow()
        with self._connect() as conn:
            conn.execute("""INSERT OR IGNORE INTO union_runs
                         (source_key, target_key, status, created_at, updated_at)
                         VALUES (?, ?, 'active', ?, ?)""", (source_key, target_key, now, now))
            return conn.execute("SELECT * FROM union_runs WHERE source_key=? AND target_key=?",
                                (source_key, target_key)).fetchone()

    def union_batches(self, run_id):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM union_batches WHERE run_id=? ORDER BY batch_index, id",
                                (run_id,)).fetchall()

    def create_union_batch(self, run_id, account_ids):
        now = self._utcnow()
        encoded = json.dumps([int(account_id) for account_id in account_ids])
        with self._connect() as conn:
            index = conn.execute("SELECT COALESCE(MAX(batch_index), 0) + 1 AS n FROM union_batches WHERE run_id=?",
                                 (run_id,)).fetchone()['n']
            conn.execute("UPDATE union_runs SET status='active', updated_at=? WHERE id=?", (now, run_id))
            conn.execute("""INSERT INTO union_batches
                         (run_id, batch_index, account_ids, status, created_at, started_at)
                         VALUES (?, ?, ?, 'active', ?, ?)""", (run_id, index, encoded, now, now))
            return conn.execute("SELECT * FROM union_batches WHERE run_id=? AND batch_index=?",
                                (run_id, index)).fetchone()

    def update_union_batch(self, batch_id, status, error=None):
        now = self._utcnow()
        safe_error = None if error is None else ' '.join(str(error).split())[:300]
        with self._connect() as conn:
            completed = now if status == 'completed' else None
            conn.execute("""UPDATE union_batches SET status=?, last_error=?,
                         completed_at=COALESCE(?, completed_at) WHERE id=?""",
                         (status, safe_error, completed, batch_id))
            row = conn.execute("SELECT run_id FROM union_batches WHERE id=?", (batch_id,)).fetchone()
            if row is not None:
                conn.execute("UPDATE union_runs SET status=?, updated_at=? WHERE id=?",
                             ('completed' if status == 'completed' else 'active', now, row['run_id']))

    def union_accounts(self, account_ids):
        ids = [int(account_id) for account_id in account_ids]
        if not ids:
            return []
        marks = ','.join('?' for _ in ids)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM accounts WHERE id IN ({}) AND status='active'".format(marks), ids).fetchall()
        by_id = dict((row['id'], row) for row in rows)
        return [by_id[account_id] for account_id in ids if account_id in by_id]

    def import_paths(
            self,
            paths,
            clean_invalid=DEFAULT_CLEAN_INVALID,
            validator=None,
            progress=None,
            skip_known_invalid=False,
            validation_workers=1):
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
        # rclone validation is network-bound and can be slow.  Parse enough to
        # schedule it up front, but retain ordered registry writes/progress.
        validation_futures = {}
        if validator is not None and int(validation_workers) > 1:
            executor = ThreadPoolExecutor(max_workers=min(int(validation_workers), len(json_paths) or 1))
            for json_path in json_paths:
                try:
                    with open(json_path, "rb") as fp:
                        payload = json.loads(fp.read().decode("utf-8"))
                    if self.validate_payload(payload) is None:
                        validation_futures[json_path] = executor.submit(validator, json_path, payload)
                except Exception:
                    pass
        else:
            executor = None
        try:
            for index, json_path in enumerate(json_paths, 1):
                self._emit_progress(progress, {
                    "event": "file",
                    "index": index,
                    "total": result.total,
                    "path": json_path,
                })
                try:
                    result.scanned += 1
                    file_validator = validator
                    if json_path in validation_futures:
                        future = validation_futures[json_path]
                        file_validator = lambda _path, _payload, future=future: future.result()
                    self._import_file(
                        json_path, clean_invalid, result, file_validator, progress,
                        index, skip_known_invalid,
                    )
                except Exception as exc:
                    reason = "import error: {}".format(exc)
                    self._record_invalid(json_path, b"", {}, None, reason, clean_invalid, result, self._utcnow())
                    self._emit_progress(progress, {
                        "event": "status", "index": index, "total": result.total,
                        "path": json_path, "status": "invalid", "reason": reason,
                    })
        finally:
            if executor is not None:
                executor.shutdown(wait=True)
        result.selected_files = sorted(set(result.selected_files))
        self._emit_progress(progress, {
            "event": "complete",
            "total": result.total,
            "result": result,
        })
        return result

    def ensure_rc_slots(self, remotes):
        """Persist the configured RC slot names without touching other remotes."""
        names = [str(remote).rstrip(":") for remote in remotes if str(remote).strip(":")]
        now = self._utcnow()
        with self._connect() as conn:
            for name in names:
                conn.execute(
                    "INSERT OR IGNORE INTO rc_slots (remote_name, account_id, updated_at) VALUES (?, NULL, ?)",
                    (name, now),
                )

    def rc_slot_account(self, remote):
        name = str(remote).rstrip(":")
        with self._connect() as conn:
            return conn.execute("""
                SELECT a.*, q.total, q.used, q.free, q.last_about_at, q.last_error
                FROM rc_slots s JOIN accounts a ON a.id=s.account_id
                LEFT JOIN quota_cache q ON q.account_id=a.id
                WHERE s.remote_name=? AND a.status='active'
            """, (name,)).fetchone()

    def empty_rc_slots(self, remotes):
        names = [str(remote).rstrip(":") for remote in remotes]
        if not names:
            return []
        marks = ','.join('?' for _ in names)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT remote_name FROM rc_slots WHERE remote_name IN ({}) AND account_id IS NULL ORDER BY remote_name".format(marks), names
            ).fetchall()
        return [row['remote_name'] for row in rows]

    def eligible_unbound_account(self, required_free):
        with self._connect() as conn:
            return conn.execute("""
                SELECT a.*, q.total, q.used, q.free, q.last_about_at, q.last_error
                FROM accounts a JOIN quota_cache q ON q.account_id=a.id
                WHERE a.status='active' AND q.free IS NOT NULL AND q.free>=?
                  AND NOT EXISTS (SELECT 1 FROM rc_slots s WHERE s.account_id=a.id)
                ORDER BY q.free DESC, a.id ASC LIMIT 1
            """, (int(required_free),)).fetchone()

    def bind_rc_slot(self, remote, account_id):
        name = str(remote).rstrip(":")
        now = self._utcnow()
        with self._connect() as conn:
            previous = conn.execute("SELECT account_id FROM rc_slots WHERE remote_name=?", (name,)).fetchone()
            if previous is not None and previous['account_id'] is not None:
                conn.execute("UPDATE accounts SET remote_name=NULL, updated_at=? WHERE id=?", (now, previous['account_id']))
            conn.execute("UPDATE accounts SET remote_name=?, updated_at=? WHERE id=?", (name, now, account_id))
            conn.execute("INSERT INTO rc_slots (remote_name, account_id, updated_at) VALUES (?, ?, ?) ON CONFLICT(remote_name) DO UPDATE SET account_id=excluded.account_id, updated_at=excluded.updated_at", (name, account_id, now))

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

    def _import_file(
            self,
            path,
            clean_invalid,
            result,
            validator=None,
            progress=None,
            index=None,
            skip_known_invalid=False):
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
        if skip_known_invalid and self._is_known_invalid(account_key):
            self._emit_status(
                progress,
                index,
                result.total,
                path,
                "skipped",
                "known invalid service account",
            )
            return
        if duplicate is not None:
            result.duplicates += 1
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

    def _is_known_invalid(self, account_key):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM accounts WHERE status='invalid' AND account_key=? LIMIT 1",
                (account_key,),
            ).fetchone()
        return row is not None

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

    def assign_stable_remote_names(self, prefix="dst", start_index=101):
        """Assign deterministic names used by a shared rclone RC server."""
        now = self._utcnow()
        with self._connect() as conn:
            for index, account in enumerate(self.active_accounts(), start_index):
                conn.execute(
                    "UPDATE accounts SET remote_name=?, updated_at=? WHERE id=?",
                    ("{}{}".format(prefix, index), now, account["id"]),
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
        last = last.replace(tzinfo=datetime.timezone.utc)
        age = datetime.datetime.now(datetime.timezone.utc) - last
        return age.total_seconds() > self.cache_ttl_hours * 3600

    def ls_cache_by_remote(self, remote, path):
        account = self.quota_by_remote(remote)
        if account is None:
            return None
        return self.ls_cache_by_account_id(account["account_id"], path)

    def ls_cache_by_account_id(self, account_id, path):
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM ls_cache WHERE account_id=? AND path=?",
                (account_id, self._normalize_cache_path(path)),
            ).fetchone()

    def should_refresh_ls_cache(self, cache_row, mode):
        if mode == "none":
            return False
        if mode == "all":
            return True
        if cache_row is None or cache_row["last_lsjson_at"] is None:
            return mode in ("missing", "stale")
        if mode == "missing":
            return False
        if mode == "stale":
            return self.is_stale(cache_row["last_lsjson_at"])
        return False

    def update_ls_cache_for_remote(self, remote, path, json_text, error=None):
        account = self.quota_by_remote(remote)
        if account is None:
            return
        self.update_ls_cache(account["account_id"], path, json_text, error)

    def update_ls_cache(self, account_id, path, json_text=None, error=None):
        now = self._utcnow()
        path = self._normalize_cache_path(path)
        if error is not None:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT account_id FROM ls_cache WHERE account_id=? AND path=?",
                    (account_id, path),
                ).fetchone()
                if existing is None:
                    conn.execute("""
                        INSERT INTO ls_cache (
                            account_id, path, json_text, last_lsjson_at, last_error, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                    """, (account_id, path, "[]", None, error, now))
                else:
                    conn.execute("""
                        UPDATE ls_cache
                        SET last_error=?, updated_at=?
                        WHERE account_id=? AND path=?
                    """, (error, now, account_id, path))
            return

        json_text = json_text or "[]"
        object_count, dir_count, file_count = self._lsjson_counts(json_text)
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO ls_cache (
                    account_id, path, json_text, object_count, dir_count, file_count,
                    last_lsjson_at, last_error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, path) DO UPDATE SET
                    json_text=excluded.json_text,
                    object_count=excluded.object_count,
                    dir_count=excluded.dir_count,
                    file_count=excluded.file_count,
                    last_lsjson_at=excluded.last_lsjson_at,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
            """, (
                account_id,
                path,
                json_text,
                object_count,
                dir_count,
                file_count,
                now,
                None,
                now,
            ))

    def ls_cache_summary(self):
        with self._connect() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*) AS cached_paths,
                    COALESCE(SUM(object_count), 0) AS objects,
                    COALESCE(SUM(file_count), 0) AS files,
                    COALESCE(SUM(dir_count), 0) AS dirs,
                    SUM(CASE WHEN last_error IS NOT NULL THEN 1 ELSE 0 END) AS errors
                FROM ls_cache
            """).fetchone()
        return row

    def invalidate_ls_cache_for_remote(self, remote):
        account = self.quota_by_remote(remote)
        if account is None:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM ls_cache WHERE account_id=?", (account["account_id"],))

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

    def delete_active_account(self, account_id, reason):
        """Disable an active account and remove its managed and source JSON files."""
        with self._connect() as conn:
            account = conn.execute(
                "SELECT source_path, managed_path FROM accounts WHERE id=? AND status='active'",
                (account_id,),
            ).fetchone()
            if account is None:
                return False
            now = self._utcnow()
            conn.execute(
                "UPDATE accounts SET status='invalid', invalid_reason=?, remote_name=NULL, updated_at=? WHERE id=?",
                (reason, now, account_id),
            )
            conn.execute("DELETE FROM quota_cache WHERE account_id=?", (account_id,))
            conn.execute("DELETE FROM ls_cache WHERE account_id=?", (account_id,))
        for path in sorted(set(path for path in (account['managed_path'], account['source_path']) if path)):
            try:
                os.remove(path)
                logging.debug(
                    'removed service account file during explicit service-account cleanup: %s',
                    path,
                )
            except FileNotFoundError:
                pass
        return True

    def mark_active_account_invalid(self, account_id, reason):
        """Disable a confirmed invalid account without removing its JSON files."""
        now = self._utcnow()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE accounts
                SET status='invalid', invalid_reason=?, remote_name=NULL, updated_at=?
                WHERE id=? AND status='active'
                """,
                (reason, now, account_id),
            )
            return cursor.rowcount > 0

    def mark_remote_quota_exhausted(self, remote):
        account = self.quota_by_remote(remote)
        if account is None:
            return
        now = self._utcnow()
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO quota_cache (
                    account_id, free, last_about_at, last_error, updated_at
                ) VALUES (?, 0, ?, NULL, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    free=0,
                    last_about_at=excluded.last_about_at,
                    last_error=NULL,
                    updated_at=excluded.updated_at
            """, (account["account_id"], now, now))

    def mark_account_quota_exhausted(self, account_id):
        now = self._utcnow()
        with self._connect() as conn:
            conn.execute("""INSERT INTO quota_cache (account_id, free, last_about_at, last_error, updated_at)
                         VALUES (?, 0, ?, NULL, ?)
                         ON CONFLICT(account_id) DO UPDATE SET free=0, last_about_at=excluded.last_about_at,
                         last_error=NULL, updated_at=excluded.updated_at""", (account_id, now, now))

    def adjust_quota_for_remote(self, remote, byte_delta):
        row = self.quota_by_remote(remote)
        if row is None:
            return
        now = self._utcnow()
        free = row["free"]
        used = row["used"]
        if free is not None:
            free = max(0, free - int(byte_delta))
            if row["total"] is not None:
                free = min(row["total"], free)
        if used is not None:
            used = max(0, used + int(byte_delta))
        with self._connect() as conn:
            conn.execute("""
                UPDATE quota_cache
                SET free=?, used=?, updated_at=?
                WHERE account_id=?
            """, (free, used, now, row["account_id"]))

    def _normalize_cache_path(self, path):
        path = path or "/"
        path = path.replace('\\', '/')
        if not path.startswith('/'):
            path = '/' + path
        while '//' in path:
            path = path.replace('//', '/')
        if len(path) > 1 and path.endswith('/'):
            path = path[:-1]
        return path

    def _lsjson_counts(self, json_text):
        try:
            rows = json.loads(json_text)
        except Exception:
            rows = []
        object_count = len(rows) if isinstance(rows, list) else 0
        dir_count = 0
        file_count = 0
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("IsDir"):
                    dir_count += 1
                else:
                    file_count += 1
        return object_count, dir_count, file_count

    @staticmethod
    def _utcnow():
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
