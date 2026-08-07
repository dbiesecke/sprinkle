import json
import io
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


progress_module = types.ModuleType("progress")
bar_module = types.ModuleType("progress.bar")
daemons_module = types.ModuleType("daemons")
prefab_module = types.ModuleType("daemons.prefab")
run_module = types.ModuleType("daemons.prefab.run")
filelock_module = types.ModuleType("filelock")


class DummyBar(object):
    def __init__(self, *args, **kwargs):
        self.message = ""

    def next(self):
        return None

    def finish(self):
        return None


bar_module.Bar = DummyBar
run_module.RunDaemon = object
filelock_module.Timeout = Exception
filelock_module.FileLock = lambda *args, **kwargs: None
sys.modules.setdefault("progress", progress_module)
sys.modules.setdefault("progress.bar", bar_module)
sys.modules.setdefault("daemons", daemons_module)
sys.modules.setdefault("daemons.prefab", prefab_module)
sys.modules.setdefault("daemons.prefab.run", run_module)
sys.modules.setdefault("filelock", filelock_module)

import sprinkle
from libsprinkle import common
from libsprinkle import clsync
from libsprinkle import rclone
from libsprinkle import service_accounts


def make_service_account(email, key_id="key-id", client_id="client-id"):
    return {
        "type": "service_account",
        "project_id": "synthetic-project",
        "private_key_id": key_id,
        "private_key": "-----BEGIN PRIVATE KEY-----\nfake-test-key\n-----END PRIVATE KEY-----\n",
        "client_email": email,
        "client_id": client_id,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/test",
        "universe_domain": "googleapis.com",
    }


def write_json(path, payload):
    with open(path, "w") as fp:
        json.dump(payload, fp)


class ServiceAccountRegistryTest(unittest.TestCase):
    def test_import_dedupes_and_quarantines_invalid_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            store = os.path.join(tmp, "store")
            db_path = os.path.join(tmp, "sa.sqlite3")
            os.mkdir(source)

            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))
            write_json(os.path.join(source, "duplicate.json"), make_service_account("one@example.test"))
            invalid = make_service_account("invalid@example.test")
            invalid.pop("client_id")
            write_json(os.path.join(source, "invalid.json"), invalid)

            registry = service_accounts.ServiceAccountRegistry(db_path, store)
            result = registry.import_paths([source])

            self.assertEqual(result.scanned, 3)
            self.assertEqual(result.imported, 1)
            self.assertEqual(result.duplicates, 1)
            self.assertEqual(result.invalid, 1)
            self.assertEqual(result.quarantined, 1)
            self.assertEqual(len(result.selected_files), 1)

            active = registry.active_accounts()
            self.assertEqual(len(active), 1)
            self.assertEqual(registry.summary_counts(), {"active": 1, "invalid": 1})
            self.assertTrue(os.path.basename(active[0]["managed_path"]).startswith("unknown-"))
            self.assertEqual(stat.S_IMODE(os.stat(active[0]["managed_path"]).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(store).st_mode), 0o700)
            self.assertEqual(len(os.listdir(os.path.join(store, "quarantine"))), 1)

    def test_reimported_duplicate_does_not_create_an_account_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            store = os.path.join(tmp, "store")
            db_path = os.path.join(tmp, "sa.sqlite3")
            os.mkdir(source)
            source_path = os.path.join(source, "one.json")
            write_json(source_path, make_service_account("one@example.test"))
            registry = service_accounts.ServiceAccountRegistry(db_path, store)

            first = registry.import_paths([source_path])
            second = registry.import_paths([source_path])

            self.assertEqual((first.imported, first.duplicates), (1, 0))
            self.assertEqual((second.imported, second.duplicates), (0, 1))
            self.assertEqual(registry.summary_counts(), {"active": 1})
            self.assertEqual(len(registry.all_account_stats()), 1)

    def test_rc_slots_reuse_configured_remote_for_next_eligible_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, 'source')
            os.mkdir(source)
            first = os.path.join(source, 'first.json')
            second = os.path.join(source, 'second.json')
            write_json(first, make_service_account('first@example.test', 'first-key'))
            write_json(second, make_service_account('second@example.test', 'second-key'))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, 'sa.sqlite3'), os.path.join(tmp, 'store')
            )
            quotas = iter(({'total': 100, 'free': 20}, {'total': 100, 'free': 80}))
            registry.import_paths([first, second], validator=lambda _path, _payload: (next(quotas), None))
            accounts = registry.active_accounts()

            registry.ensure_rc_slots(['dst101:'])
            registry.bind_rc_slot('dst101:', accounts[0]['id'])
            self.assertEqual(registry.rc_slot_account('dst101:')['id'], accounts[0]['id'])
            self.assertEqual(registry.eligible_unbound_account(50)['id'], accounts[1]['id'])

            registry.bind_rc_slot('dst101:', accounts[1]['id'])
            self.assertEqual(registry.rc_slot_account('dst101:')['id'], accounts[1]['id'])
            self.assertEqual(registry.eligible_unbound_account(50), None)

    def test_quota_error_preserves_cached_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))

            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"),
                os.path.join(tmp, "store"),
            )
            registry.import_paths([source])
            account = registry.active_accounts()[0]

            registry.update_quota(account["id"], {"total": 100, "used": 60, "free": 40}, None)
            registry.update_quota(account["id"], None, "rclone about failed")
            quota = registry.quota_by_account_id(account["id"])

            self.assertEqual(quota["total"], 100)
            self.assertEqual(quota["used"], 60)
            self.assertEqual(quota["free"], 40)
            self.assertEqual(quota["last_error"], "rclone about failed")

    def test_quota_error_cannot_reuse_cached_capacity_or_refresh_before_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"), os.path.join(tmp, "store"),
            )
            registry.import_paths([source])
            account = registry.active_accounts()[0]
            registry.update_quota(account["id"], {"total": 100, "used": 10, "free": 90}, None)
            registry.update_quota(
                account["id"], None,
                "rclone about returned unknown quota: missing total,free",
            )

            quota = registry.quota_by_account_id(account["id"])
            self.assertFalse(service_accounts.has_usable_quota(quota))
            self.assertIsNone(registry.eligible_unbound_account(1))
            self.assertFalse(registry.should_refresh(quota, "stale"))
            self.assertFalse(registry.should_refresh(quota, "missing"))
            with registry._connect() as conn:
                conn.execute(
                    "UPDATE quota_cache SET updated_at='2000-01-01T00:00:00Z' WHERE account_id=?",
                    (account["id"],),
                )
            self.assertTrue(registry.should_refresh(registry.quota_by_account_id(account["id"]), "stale"))

    def test_cached_unknown_quota_does_not_call_rclone_before_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"), os.path.join(tmp, "store"),
            )
            registry.import_paths([source])
            account = registry.active_accounts()[0]
            registry.assign_remote_names([{"remote": "dst101", "path": account["managed_path"]}])
            registry.update_quota(account["id"], None, "rclone about returned unknown quota: missing free")
            calls = []
            sync = clsync.ClSync.__new__(clsync.ClSync)
            sync._sa_registry = registry
            sync._sa_refresh = "stale"
            sync._rclone = types.SimpleNamespace(
                get_about_json_with_error=lambda _remote: calls.append(_remote) or ({"total": 100, "free": 99}, None)
            )

            self.assertIsNone(sync._get_remote_quota("dst101:"))
            self.assertEqual(calls, [])

    def test_union_assumes_15_gib_without_about_and_normal_backup_stays_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"), os.path.join(tmp, "store"),
            )
            registry.import_paths([source])
            account = registry.active_accounts()[0]
            old_refresh = sprinkle._refresh_service_account_quota
            old_config = getattr(sprinkle, "__config", None)
            try:
                setattr(sprinkle, "__config", {"sa_refresh": "none", "sa_delete_rc_http_500": False})
                sprinkle._refresh_service_account_quota = lambda _account: self.fail("fresh unknown Union quota must not About")
                self.assertEqual(sprinkle._backup_accounts_with_free_space(registry, [account]), [])
                self.assertEqual(sprinkle._union_accounts_with_free_space(registry, [account], 1), [account])
            finally:
                sprinkle._refresh_service_account_quota = old_refresh
                setattr(sprinkle, "__config", old_config)

            quota = registry.quota_by_account_id(account["id"])
            self.assertEqual(quota["quota_state"], service_accounts.QUOTA_STATE_UNKNOWN_ASSUMED)
            self.assertEqual(quota["total"], 15 * 1024 ** 3)
            self.assertEqual(quota["free"], 15 * 1024 ** 3)

    def test_stale_union_unknown_states_are_rechecked_but_fresh_full_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"), os.path.join(tmp, "store"), cache_ttl_hours=1,
            )
            registry.import_paths([source])
            account = registry.active_accounts()[0]
            registry.assume_unknown_quota_for_union(account["id"])
            registry.mark_union_unknown_accounts_full([account["id"]], "any transfer failure")
            calls = []
            old_refresh = sprinkle._refresh_service_account_quota
            try:
                sprinkle._refresh_service_account_quota = lambda _account: calls.append(1) or ({"total": 100, "used": 1, "free": 99}, None)
                self.assertEqual(sprinkle._union_accounts_with_free_space(registry, [account], 1), [])
                self.assertEqual(calls, [])
                with registry._connect() as conn:
                    conn.execute("UPDATE quota_cache SET updated_at='2000-01-01T00:00:00Z' WHERE account_id=?", (account["id"],))
                self.assertEqual(sprinkle._union_accounts_with_free_space(registry, [account], 1), [account])
            finally:
                sprinkle._refresh_service_account_quota = old_refresh
            quota = registry.quota_by_account_id(account["id"])
            self.assertEqual(calls, [1])
            self.assertEqual(quota["quota_state"], service_accounts.QUOTA_STATE_KNOWN)

    def test_union_transfer_failure_marks_only_assumed_accounts_unknown_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            write_json(os.path.join(source, "known.json"), make_service_account("known@example.test", "known"))
            write_json(os.path.join(source, "unknown.json"), make_service_account("unknown@example.test", "unknown"))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"), os.path.join(tmp, "store"),
            )
            registry.import_paths([source])
            known, unknown = registry.active_accounts()
            registry.update_quota(known["id"], {"total": 100, "used": 20, "free": 80})
            registry.assume_unknown_quota_for_union(unknown["id"])

            self.assertEqual(registry.mark_union_unknown_accounts_full(
                [known["id"], unknown["id"]], "temporary transfer error"), 1)
            known_quota = registry.quota_by_account_id(known["id"])
            unknown_quota = registry.quota_by_account_id(unknown["id"])
            self.assertEqual((known_quota["quota_state"], known_quota["free"]),
                             (service_accounts.QUOTA_STATE_KNOWN, 80))
            self.assertEqual((unknown_quota["quota_state"], unknown_quota["free"]),
                             (service_accounts.QUOTA_STATE_UNKNOWN_FULL, 0))
            self.assertTrue(unknown_quota["last_error"].startswith("UNKNOWN-FULL:"))

    def test_backup_union_rotates_after_any_transfer_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = os.path.join(tmp, "source")
            accounts_dir = os.path.join(tmp, "accounts")
            os.mkdir(source_dir)
            os.mkdir(accounts_dir)
            with open(os.path.join(source_dir, "rom.bin"), "wb") as fp:
                fp.write(b"test")
            write_json(os.path.join(accounts_dir, "first.json"), make_service_account("first@example.test", "first"))
            write_json(os.path.join(accounts_dir, "second.json"), make_service_account("second@example.test", "second"))
            db_path = os.path.join(tmp, "sa.sqlite3")
            store = os.path.join(tmp, "store")
            registry = service_accounts.ServiceAccountRegistry(db_path, store)
            registry.import_paths([accounts_dir])
            old_config = getattr(sprinkle, "__config", None)
            old_args = getattr(sprinkle, "__args", None)
            old_rclone = sprinkle.rclone.RClone
            old_sample = sprinkle.random.sample

            class FakeRClone(object):
                attempts = 0

                def __init__(self, *_args, **_kwargs):
                    pass

                def configure_rc_drive_service_account(self, *_args, **_kwargs):
                    pass

                def configure_rc_union(self, *_args, **_kwargs):
                    pass

                def delete_rc_remote(self, *_args, **_kwargs):
                    pass

                def transfer(self, *_args, **_kwargs):
                    FakeRClone.attempts += 1
                    if FakeRClone.attempts == 1:
                        raise Exception("temporary union transfer failure")

            try:
                setattr(sprinkle, "__config", {
                    "drive_id": "synthetic-drive", "rclone_sa_dir": None,
                    "sa_db": db_path, "sa_store": store, "sa_cache_ttl_hours": 72,
                    "rclone_sa_count": 1, "rclone_rc_url": "http://rc.test",
                    "rclone_rc_user": None, "rclone_rc_password": None,
                    "rclone_rc_timeout_seconds": 1, "rclone_exe": "rclone",
                    "rclone_retries": "1", "rclone_move": False,
                    "delete_files": False, "dry_run": True,
                })
                setattr(sprinkle, "__args", ["backup-union", source_dir])
                sprinkle.rclone.RClone = FakeRClone
                sprinkle.random.sample = lambda candidates, count: candidates[:count]
                sprinkle.backup_union()
            finally:
                sprinkle.rclone.RClone = old_rclone
                sprinkle.random.sample = old_sample
                setattr(sprinkle, "__config", old_config)
                setattr(sprinkle, "__args", old_args)

            batches = registry.union_batches(registry.union_run(source_dir, "")["id"])
            self.assertEqual([batch["status"] for batch in batches], ["exhausted", "completed"])
            self.assertEqual(FakeRClone.attempts, 2)
            first, second = registry.active_accounts()
            self.assertEqual(registry.quota_by_account_id(first["id"])["quota_state"],
                             service_accounts.QUOTA_STATE_UNKNOWN_FULL)
            self.assertEqual(registry.quota_by_account_id(second["id"])["quota_state"],
                             service_accounts.QUOTA_STATE_UNKNOWN_ASSUMED)

    def test_union_never_reassumes_confirmed_full_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"), os.path.join(tmp, "store"),
            )
            registry.import_paths([source])
            account = registry.active_accounts()[0]
            registry.update_quota(account["id"], {"total": 100, "used": 100, "free": 0})

            self.assertEqual(sprinkle._union_accounts_with_free_space(registry, [account], 1), [])
            quota = registry.quota_by_account_id(account["id"])
            self.assertEqual((quota["quota_state"], quota["free"]),
                             (service_accounts.QUOTA_STATE_KNOWN, 0))

    def test_quota_delta_clamps_and_keeps_about_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"), os.path.join(tmp, "store"),
            )
            registry.import_paths([source])
            account = registry.active_accounts()[0]
            registry.assign_remote_names([{"remote": "dst101", "path": account["managed_path"]}])
            registry.update_quota(account["id"], {"total": 100, "used": 20, "free": 80}, None)
            before = registry.quota_by_account_id(account["id"])["last_about_at"]

            registry.adjust_quota_for_remote("dst101:", 30)
            registry.adjust_quota_for_remote("dst101:", -500)

            quota = registry.quota_by_account_id(account["id"])
            self.assertEqual(quota["used"], 0)
            self.assertEqual(quota["free"], 100)
            self.assertEqual(quota["last_about_at"], before)

    def test_confirmed_transfer_applies_actual_add_update_and_remove_deltas(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"), os.path.join(tmp, "store"),
            )
            registry.import_paths([source])
            account = registry.active_accounts()[0]
            registry.assign_remote_names([{"remote": "dst101", "path": account["managed_path"]}])
            registry.update_quota(account["id"], {"total": 1000, "used": 100, "free": 900}, None)
            sync = clsync.ClSync.__new__(clsync.ClSync)
            sync._distribution_type = "mas"
            sync._cached_free = {"dst101:": 900}
            sync._frees = None
            sync._sa_registry = registry
            sync._clear_memory_ls_cache = lambda: None
            sync._rclone = types.SimpleNamespace(
                lsjson=lambda *_args: json.dumps([{"Name": "movie.mkv", "Size": 150, "IsDir": False}])
            )

            sync._record_confirmed_transfer("dst101:", "/movies", "movie.mkv", 0, 150)
            sync._rclone.lsjson = lambda *_args: json.dumps([{"Name": "movie.mkv", "Size": 180, "IsDir": False}])
            sync._record_confirmed_transfer("dst101:", "/movies", "movie.mkv", 150, 180)
            sync.mark_remote_used("dst101:", -180)

            quota = registry.quota_by_remote("dst101:")
            self.assertEqual(quota["used"], 100)
            self.assertEqual(quota["free"], 900)

    def test_unconfirmed_transfer_does_not_change_quota_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"), os.path.join(tmp, "store"),
            )
            registry.import_paths([source])
            account = registry.active_accounts()[0]
            registry.assign_remote_names([{"remote": "dst101", "path": account["managed_path"]}])
            registry.update_quota(account["id"], {"total": 100, "used": 10, "free": 90}, None)
            sync = clsync.ClSync.__new__(clsync.ClSync)
            sync._sa_registry = registry
            sync._rclone = types.SimpleNamespace(lsjson=lambda *_args: "[]")

            with self.assertRaisesRegex(Exception, "target file not found"):
                sync._record_confirmed_transfer("dst101:", "/movies", "movie.mkv", 0, 50)

            quota = registry.quota_by_remote("dst101:")
            self.assertEqual((quota["used"], quota["free"]), (10, 90))

    def test_storage_quota_error_marks_remote_cache_as_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"),
                os.path.join(tmp, "store"),
            )
            registry.import_paths([source])
            account = registry.active_accounts()[0]
            registry.assign_remote_names([{"remote": "dst101", "path": account["managed_path"]}])
            registry.update_quota(account["id"], {"total": 100, "used": 100, "free": 0}, None)

            registry.mark_remote_quota_exhausted("dst101:")

            quota = registry.quota_by_remote("dst101:")
            self.assertEqual(quota["free"], 0)
            self.assertIsNone(quota["last_error"])

    def test_account_not_found_cleanup_requires_explicit_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            source_file = os.path.join(source, "one.json")
            write_json(source_file, make_service_account("one@example.test"))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"),
                os.path.join(tmp, "store"),
            )
            registry.import_paths([source])
            account = registry.active_accounts()[0]
            managed_file = account["managed_path"]
            old_config = getattr(sprinkle, "__config", None)
            error = '{"error_description":"Invalid grant: account not found"}'

            try:
                setattr(sprinkle, "__config", {"sa_delete_account_not_found": False})
                self.assertFalse(sprinkle._delete_account_not_found_if_requested(registry, account, error))
                self.assertTrue(os.path.exists(source_file))
                self.assertTrue(os.path.exists(managed_file))

                setattr(sprinkle, "__config", {"sa_delete_account_not_found": True})
                with self.assertLogs(level="DEBUG") as logs:
                    self.assertTrue(sprinkle._delete_account_not_found_if_requested(registry, account, error))
            finally:
                setattr(sprinkle, "__config", old_config)

            self.assertFalse(os.path.exists(source_file))
            self.assertFalse(os.path.exists(managed_file))
            self.assertEqual(len(registry.active_accounts()), 0)
            self.assertEqual(registry.all_account_stats()[0]["status"], "invalid")
            output = "\n".join(logs.output)
            self.assertIn("removed service account file during explicit service-account cleanup: " + source_file, output)
            self.assertIn("removed service account file during explicit service-account cleanup: " + managed_file, output)

    def test_account_not_found_detection_rejects_other_invalid_grants(self):
        self.assertTrue(sprinkle._is_account_not_found_error("Invalid grant: account not found"))
        self.assertFalse(sprinkle._is_account_not_found_error("Invalid grant: Invalid JWT Signature"))

    def test_rc_http_500_cleanup_is_explicit_and_refreshes_all_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            source_file = os.path.join(source, "one.json")
            write_json(source_file, make_service_account("one@example.test"))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"), os.path.join(tmp, "store")
            )
            registry.import_paths([source])
            account = registry.active_accounts()[0]
            managed_file = account["managed_path"]
            old_config = getattr(sprinkle, "__config", None)
            old_refresh = sprinkle._refresh_service_account_quota
            try:
                setattr(sprinkle, "__config", {
                    "sa_refresh": "none",
                    "sa_delete_rc_http_500": True,
                })
                sprinkle._refresh_service_account_quota = lambda _account: (
                    None, "rclone RC operations/about failed: HTTP 500"
                )
                self.assertEqual(sprinkle._backup_accounts_with_free_space(registry, [account]), [])
            finally:
                sprinkle._refresh_service_account_quota = old_refresh
                setattr(sprinkle, "__config", old_config)

            self.assertFalse(os.path.exists(source_file))
            self.assertFalse(os.path.exists(managed_file))
            self.assertEqual(len(registry.active_accounts()), 0)

    def test_account_not_found_marks_account_invalid_without_delete_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"),
                os.path.join(tmp, "store"),
            )
            registry.import_paths([source])
            account = registry.active_accounts()[0]
            old_config = getattr(sprinkle, "__config", None)

            try:
                setattr(sprinkle, "__config", {"sa_delete_account_not_found": False})
                self.assertTrue(sprinkle._handle_account_not_found(
                    registry,
                    account,
                    "Invalid grant: account not found",
                ))
            finally:
                setattr(sprinkle, "__config", old_config)

            self.assertEqual(registry.all_account_stats()[0]["status"], "invalid")
            self.assertTrue(os.path.exists(os.path.join(source, "one.json")))

    def test_import_validator_stores_quota_and_reports_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))
            events = []

            def validator(_path, _payload):
                return {"total": 100, "used": 25, "free": 75}, None

            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"),
                os.path.join(tmp, "store"),
            )
            result = registry.import_paths([source], validator=validator, progress=events.append)
            account = registry.active_accounts()[0]
            quota = registry.quota_by_account_id(account["id"])

            self.assertEqual(result.total, 1)
            self.assertEqual(result.validated, 1)
            self.assertEqual(result.imported, 1)
            self.assertEqual(quota["total"], 100)
            self.assertEqual(quota["free"], 75)
            self.assertEqual(events[0]["event"], "start")
            self.assertEqual(events[-1]["event"], "complete")
            self.assertTrue(any(event.get("status") == "imported" for event in events))

    def test_import_validator_unknown_quarantines_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            write_json(os.path.join(source, "unknown.json"), make_service_account("unknown@example.test"))

            def validator(_path, _payload):
                return None, "rclone about returned unknown quota: missing free"

            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"),
                os.path.join(tmp, "store"),
            )
            result = registry.import_paths([source], validator=validator)
            rows = registry.all_account_stats()

            self.assertEqual(result.imported, 0)
            self.assertEqual(result.invalid, 1)
            self.assertEqual(result.validation_errors, 1)
            self.assertEqual(result.quarantined, 1)
            self.assertEqual(len(registry.active_accounts()), 0)
            self.assertEqual(rows[0]["status"], "invalid")
            self.assertIn("unknown quota", rows[0]["invalid_reason"])
            self.assertEqual(len(os.listdir(os.path.join(tmp, "store", "quarantine"))), 1)

    def test_drive_api_service_disabled_moves_files_and_removes_sqlite_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            source_path = os.path.join(source, "disabled.json")
            write_json(source_path, make_service_account("disabled@example.test"))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"), os.path.join(tmp, "store"),
            )
            registry.import_paths([source_path])
            account = registry.active_accounts()[0]
            managed_path = account["managed_path"]
            registry.update_quota(account["id"], {"total": 100, "used": 1, "free": 99}, None)

            error = "googleapi: Error 403, accessNotConfigured: Google Drive API has not been used"
            self.assertTrue(sprinkle._handle_service_deactivated(registry, account, error))

            self.assertEqual(registry.active_accounts(), [])
            self.assertEqual(registry.all_account_stats(), [])
            self.assertFalse(os.path.exists(source_path))
            self.assertFalse(os.path.exists(managed_path))
            deactivated_dir = os.path.join(tmp, "store", "service-deactivated")
            self.assertEqual(len(os.listdir(deactivated_dir)), 2)
            self.assertTrue(all(name.startswith("service-deactivated-") for name in os.listdir(deactivated_dir)))

    def test_service_deactivated_directory_is_not_reimported(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "store")
            registry = service_accounts.ServiceAccountRegistry(os.path.join(tmp, "sa.sqlite3"), store)
            deactivated = os.path.join(store, "service-deactivated", "disabled.json")
            write_json(deactivated, make_service_account("disabled@example.test"))

            result = registry.import_paths([store])

            self.assertEqual(result.scanned, 0)
            self.assertEqual(result.imported, 0)
            self.assertEqual(registry.active_accounts(), [])

    def test_missing_managed_file_removes_account_and_sqlite_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            source_path = os.path.join(source, "removed.json")
            write_json(source_path, make_service_account("removed@example.test"))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"), os.path.join(tmp, "store"),
            )
            registry.import_paths([source_path])
            account = registry.active_accounts()[0]
            registry.update_quota(account["id"], {"total": 100, "used": 1, "free": 99}, None)
            registry.ensure_rc_slots(["dst101:"])
            registry.bind_rc_slot("dst101:", account["id"])
            os.remove(account["managed_path"])

            self.assertEqual(registry.remove_missing_file_accounts(), [account["id"]])

            self.assertEqual(registry.active_accounts(), [])
            self.assertEqual(registry.all_account_stats(), [])
            self.assertEqual(registry.empty_rc_slots(["dst101:"]), ["dst101"])

    def test_missing_source_file_removes_account_from_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            source_path = os.path.join(source, "removed.json")
            write_json(source_path, make_service_account("removed@example.test"))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"), os.path.join(tmp, "store"),
            )
            registry.import_paths([source_path])
            account = registry.active_accounts()[0]
            os.remove(source_path)

            self.assertEqual(registry.remove_missing_file_accounts(), [account["id"]])
            self.assertEqual(registry.active_accounts(), [])
            self.assertTrue(os.path.isfile(account["managed_path"]))

    def test_existing_unknown_managed_file_is_renamed_to_unknown_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            os.mkdir(source)
            source_path = os.path.join(source, "one.json")
            write_json(source_path, make_service_account("one@example.test"))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, "sa.sqlite3"), os.path.join(tmp, "store"),
            )
            registry.import_paths([source_path])
            account = registry.active_accounts()[0]
            old_path = os.path.join(os.path.dirname(account["managed_path"]), "sa-legacy.json")
            os.rename(account["managed_path"], old_path)
            with registry._connect() as conn:
                conn.execute("UPDATE accounts SET managed_path=? WHERE id=?", (old_path, account["id"]))

            registry.remove_missing_file_accounts()

            migrated = registry.active_accounts()[0]
            self.assertTrue(os.path.basename(migrated["managed_path"]).startswith("unknown-"))
            self.assertTrue(os.path.isfile(migrated["managed_path"]))
            self.assertFalse(os.path.exists(old_path))


class RCloneQuotaTest(unittest.TestCase):
    def test_about_json_is_reused_for_size_and_free(self):
        calls = []
        old_execute = common.execute

        def fake_execute(command, no_error=False):
            calls.append(command)
            return {
                "code": 0,
                "out": json.dumps({"total": 100, "used": 25, "free": 75}),
                "error": "",
            }

        try:
            common.execute = fake_execute
            rc = rclone.RClone()
            self.assertEqual(rc.get_size("dst101:"), 100)
            self.assertEqual(rc.get_free("dst101:"), 75)
            self.assertEqual(calls[0][1], "about")
            self.assertIn("--json", calls[0])
        finally:
            common.execute = old_execute

    def test_about_json_with_error_preserves_rclone_stderr(self):
        old_execute = common.execute

        def fake_execute(_command, no_error=False):
            return {
                "code": 1,
                "out": "",
                "error": "invalid_grant: Invalid JWT Signature",
            }

        try:
            common.execute = fake_execute
            rc = rclone.RClone()
            quota, error = rc.get_about_json_with_error("dst101:")
        finally:
            common.execute = old_execute

        self.assertIsNone(quota)
        self.assertIn("invalid_grant", error)
        friendly = sprinkle._friendly_rclone_error(
            error,
            {"client_email": "one@example.test", "project_id": "project-one"},
        )
        self.assertIn("credentials rejected", friendly)
        self.assertIn("one@example.test", friendly)

    def test_rc_http_error_preserves_quota_error_body(self):
        response = io.BytesIO(json.dumps({
            "error": "googleapi: Error 403: storageQuotaExceeded",
        }).encode("utf-8"))
        failure = rclone.urllib_error.HTTPError(
            "https://rc.example.test/sync/move", 500, "Internal Server Error", None, response
        )
        rc = rclone.RClone(rc_url="https://rc.example.test")

        with mock.patch.object(rclone.urllib_request, "urlopen", side_effect=failure):
            with self.assertRaisesRegex(Exception, "storageQuotaExceeded") as raised:
                rc.move("/source/movie.mkv", "dst101:/movies")

        self.assertIn("HTTP 500", str(raised.exception))

    def test_lsjson_ignores_rclone_progress_output(self):
        old_execute = common.execute

        def fake_execute(_command, no_error=False):
            return {
                "code": 0,
                "out": "[\n]\nTransferred:   \t          0 B / 0 B, -, 0 B/s, ETA -\nElapsed time:         1.7s\n",
                "error": "",
            }

        try:
            common.execute = fake_execute
            rc = rclone.RClone()
            out = rc.lsjson("dst101:", "/Movies/Aladin", ["--fast-list"], True)
        finally:
            common.execute = old_execute

        self.assertEqual(json.loads(out), [])

    def test_about_json_ignores_rclone_progress_output(self):
        old_execute = common.execute

        def fake_execute(_command, no_error=False):
            return {
                "code": 0,
                "out": '{"total": 100, "free": 75}\nTransferred:   \t0 B / 0 B, -, 0 B/s, ETA -\n',
                "error": "",
            }

        try:
            common.execute = fake_execute
            rc = rclone.RClone()
            quota, error = rc.get_about_json_with_error("dst101:")
        finally:
            common.execute = old_execute

        self.assertIsNone(error)
        self.assertEqual(quota["free"], 75)

    def test_unknown_quota_reason_requires_total_and_free(self):
        self.assertIn("missing total,free", sprinkle._quota_unknown_reason({"used": 1}))
        self.assertIsNone(sprinkle._quota_unknown_reason({"total": 100, "free": 0}))

    def test_generate_rclone_config_from_explicit_files_returns_remote_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            sa_file = os.path.join(tmp, "account.json")
            write_json(sa_file, make_service_account("one@example.test"))
            out = os.path.join(tmp, "rclone.conf")

            content, entries = rclone.generate_rclone_config_from_files(
                [sa_file],
                out,
                "drive-id",
                start_index=1,
                return_entries=True,
            )

            self.assertEqual(entries, [{"remote": "dst1", "path": sa_file}])
            self.assertIn("service_account_file = " + sa_file, content)
            self.assertIn("root_folder_id = drive-id", content)

    def test_generate_rclone_config_includes_base_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            sa_file = os.path.join(tmp, "account.json")
            base_conf = os.path.join(tmp, "base-rclone.conf")
            out = os.path.join(tmp, "rclone.conf")
            write_json(sa_file, make_service_account("one@example.test"))
            with open(base_conf, "w") as fp:
                fp.write("[hidrive]\ntype = local\n")

            content, entries = rclone.generate_rclone_config_from_files(
                [sa_file],
                out,
                "drive-id",
                start_index=1,
                return_entries=True,
                base_config_file=base_conf,
            )

            self.assertEqual(entries, [{"remote": "dst1", "path": sa_file}])
            self.assertIn("[hidrive]", content)
            self.assertIn("[dst1]", content)

    def test_generate_rclone_config_can_disable_shuffle_for_stable_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "b.json")
            second = os.path.join(tmp, "a.json")
            write_json(first, make_service_account("b@example.test", "key-b"))
            write_json(second, make_service_account("a@example.test", "key-a"))
            out = os.path.join(tmp, "rclone.conf")

            content, entries = rclone.generate_rclone_config_from_files(
                [first, second],
                out,
                "drive-id",
                max_accounts=1,
                start_index=1,
                return_entries=True,
                shuffle=False,
            )

            self.assertEqual(entries, [{"remote": "dst1", "path": second}])
            self.assertIn("service_account_file = " + second, content)

    def test_generate_combine_config_groups_local_upstreams(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "combine.conf")
            content = rclone.generate_rclone_combine_config(
                ["one={}".format(os.path.join(tmp, "one")), "two={}".format(os.path.join(tmp, "two"))],
                out,
                group_size=1,
            )

            self.assertIn("[sa_group1]", content)
            self.assertIn("[sa_group2]", content)
            self.assertIn("type = combine", content)
            self.assertIn("upstreams = one=", content)


class ClSyncPlacementTest(unittest.TestCase):
    def test_missing_file_comparison_uses_single_debug_line(self):
        sync = clsync.ClSync.__new__(clsync.ClSync)
        sync._compare_method = "size"
        local_file = types.SimpleNamespace(
            path="./roms/SPC",
            name="Super Trump Collection [01-Title Screen][n].spc",
            size=1,
            is_dir=False,
        )

        with self.assertLogs(level="DEBUG") as logs:
            operations = sync.compare_clfiles_for_remote_root(
                "./roms",
                {"./roms/SPC/Super Trump Collection [01-Title Screen][n].spc": local_file},
                {},
                delete_file=False,
                remote_root="/roms",
            )

        compare_logs = [line for line in logs.output if "compare file local=" in line]
        self.assertEqual(len(operations), 1)
        self.assertEqual(len(compare_logs), 1)
        self.assertIn("result=add", compare_logs[0])
        self.assertFalse(any("remote name:" in line for line in logs.output))

    def test_lsjson_results_are_cached_by_service_account_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            store = os.path.join(tmp, "store")
            db_path = os.path.join(tmp, "sa.sqlite3")
            os.mkdir(source)
            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))
            registry = service_accounts.ServiceAccountRegistry(db_path, store)
            registry.import_paths([source])
            account = registry.active_accounts()[0]
            registry.assign_remote_names([{"remote": "dst101", "path": account["managed_path"]}])
            calls = []
            payload = json.dumps([{
                "Path": "movie.mkv",
                "Name": "movie.mkv",
                "Size": 10,
                "MimeType": "video/x-matroska",
                "ModTime": "2024-01-01T00:00:00Z",
                "IsDir": False,
                "ID": "file-id",
            }])

            def make_sync():
                sync = clsync.ClSync.__new__(clsync.ClSync)
                sync._config = {
                    "no_cache": False,
                    "ls_stop_first": False,
                }
                sync._sa_registry = service_accounts.ServiceAccountRegistry(db_path, store)
                sync._sa_refresh = "stale"
                sync._compare_method = "size"
                sync._cache = {}
                sync._cache_counter = {}
                sync._cache_invalidation_max = 10
                sync.get_remotes = lambda: ["dst101:"]
                sync._rclone = types.SimpleNamespace(
                    lsjson=lambda remote, path, _args, _no_error: calls.append((remote, path)) or payload
                )
                return sync

            first = make_sync()
            self.assertIn("/Movies/movie.mkv", first.ls("/Movies"))
            second = make_sync()
            self.assertIn("/Movies/movie.mkv", second.ls("/Movies"))
            self.assertEqual(calls, [("dst101:", "/Movies")])

    def test_root_lsjson_cache_serves_subdirectory_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            store = os.path.join(tmp, "store")
            db_path = os.path.join(tmp, "sa.sqlite3")
            os.mkdir(source)
            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))
            registry = service_accounts.ServiceAccountRegistry(db_path, store)
            registry.import_paths([source])
            account = registry.active_accounts()[0]
            registry.assign_remote_names([{"remote": "dst101", "path": account["managed_path"]}])
            registry.update_ls_cache(account["id"], "/", json.dumps([{
                "Path": "Movies/Aladin/movie.mkv",
                "Name": "movie.mkv",
                "Size": 10,
                "MimeType": "video/x-matroska",
                "ModTime": "2024-01-01T00:00:00Z",
                "IsDir": False,
                "ID": "file-id",
            }]))
            calls = []
            sync = clsync.ClSync.__new__(clsync.ClSync)
            sync._config = {
                "no_cache": False,
                "ls_stop_first": False,
            }
            sync._sa_registry = service_accounts.ServiceAccountRegistry(db_path, store)
            sync._sa_refresh = "stale"
            sync._compare_method = "size"
            sync._cache = {}
            sync._cache_counter = {}
            sync._cache_invalidation_max = 10
            sync.get_remotes = lambda: ["dst101:"]
            sync._rclone = types.SimpleNamespace(
                lsjson=lambda remote, path, _args, _no_error: calls.append((remote, path)) or "[]"
            )

            files = sync.ls("/Movies/Aladin")

            self.assertIn("/Movies/Aladin/movie.mkv", files)
            self.assertEqual(calls, [])

    def test_drive_id_ls_stop_first_stops_after_empty_listing(self):
        sync = clsync.ClSync.__new__(clsync.ClSync)
        sync._config = {
            "no_cache": False,
            "ls_stop_first": True,
            "drive_id": "drive-id",
        }
        sync._sa_registry = None
        sync._sa_refresh = "stale"
        sync._compare_method = "size"
        sync._cache = {}
        sync._cache_counter = {}
        sync._cache_invalidation_max = 10
        sync.get_remotes = lambda: ["dst101:", "dst102:", "dst103:"]
        calls = []
        sync._rclone = types.SimpleNamespace(
            lsjson=lambda remote, path, _args, _no_error: calls.append((remote, path)) or "[]"
        )

        files = sync.ls("/Movies/Aladin")

        self.assertEqual(files, {})
        self.assertEqual(calls, [("dst101:", "/Movies/Aladin")])

    def test_ls_shallow_omits_recursive_rclone_arg(self):
        sync = clsync.ClSync.__new__(clsync.ClSync)
        sync._config = {
            "no_cache": False,
            "ls_stop_first": True,
        }
        sync._sa_registry = None
        sync._sa_refresh = "stale"
        sync._compare_method = "size"
        sync._cache = {}
        sync._cache_counter = {}
        sync._cache_invalidation_max = 10
        sync.get_remotes = lambda: ["dst101:"]
        calls = []
        payload = json.dumps([{
            "Path": "movie.mkv",
            "Name": "movie.mkv",
            "Size": 10,
            "MimeType": "video/x-matroska",
            "ModTime": "2024-01-01T00:00:00Z",
            "IsDir": False,
            "ID": "file-id",
        }])
        sync._rclone = types.SimpleNamespace(
            lsjson=lambda remote, path, args, _no_error: calls.append((remote, path, args)) or payload
        )

        files = sync.ls_shallow("/Movies/Aladin")

        self.assertIn("/Movies/Aladin/movie.mkv", files)
        self.assertEqual(calls, [("dst101:", "/Movies/Aladin", ["--fast-list"])])

    def test_lsjson_accepts_files_and_directories_without_ids(self):
        sync = clsync.ClSync.__new__(clsync.ClSync)
        sync._config = {
            "no_cache": False,
            "ls_stop_first": False,
        }
        sync._sa_registry = None
        sync._sa_refresh = "stale"
        sync._compare_method = "size"
        sync._cache = {}
        sync._cache_counter = {}
        sync._cache_invalidation_max = 10
        sync.get_remotes = lambda: ["local:"]
        payload = json.dumps([
            {
                "Path": "movie.mkv",
                "Name": "movie.mkv",
                "Size": 10,
                "MimeType": "video/x-matroska",
                "ModTime": "2024-01-01T00:00:00Z",
                "IsDir": False,
            },
            {
                "Path": "extras",
                "Name": "extras",
                "Size": -1,
                "MimeType": "inode/directory",
                "ModTime": "2024-01-01T00:00:00Z",
                "IsDir": True,
            },
            {
                "Path": "drive-file.mkv",
                "Name": "drive-file.mkv",
                "Size": 20,
                "MimeType": "video/x-matroska",
                "ModTime": "2024-01-01T00:00:00Z",
                "IsDir": False,
                "ID": "drive-file-id",
            },
        ])
        sync._rclone = types.SimpleNamespace(lsjson=lambda *_args: payload)

        files = sync.ls("/Movies/Aladin")

        self.assertIsNone(files["/Movies/Aladin/movie.mkv"].id)
        self.assertIsNone(files["/Movies/Aladin/extras"].id)
        self.assertEqual(files["/Movies/Aladin/drive-file.mkv"].id, "drive-file-id")

    def test_backup_with_delete_files_removes_idless_file_and_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            os.makedirs(source)
            os.makedirs(target)
            keep_path = os.path.join(source, "keep.txt")
            with open(keep_path, "w") as fp:
                fp.write("keep")

            sync = clsync.ClSync.__new__(clsync.ClSync)
            sync._show_progress = False
            sync._compare_method = "size"
            sync._ClSync__exclusion_list = None
            sync._ClSync__exclude_regex = None
            sync._config = {"no_cache": True, "ls_stop_first": False}
            sync._sa_registry = None
            sync._sa_refresh = "stale"
            sync._cache = {}
            sync._cache_counter = {}
            sync._cache_invalidation_max = 10
            payload = json.dumps([
                {
                    "Path": "keep.txt",
                    "Name": "keep.txt",
                    "Size": 4,
                    "MimeType": "text/plain",
                    "ModTime": "2024-01-01T00:00:00Z",
                    "IsDir": False,
                },
                {
                    "Path": "removed.txt",
                    "Name": "removed.txt",
                    "Size": 7,
                    "MimeType": "text/plain",
                    "ModTime": "2024-01-01T00:00:00Z",
                    "IsDir": False,
                },
                {
                    "Path": "orphan-dir",
                    "Name": "orphan-dir",
                    "Size": -1,
                    "MimeType": "inode/directory",
                    "ModTime": "2024-01-01T00:00:00Z",
                    "IsDir": True,
                },
            ])
            sync._rclone = types.SimpleNamespace(lsjson=lambda *_args: payload)
            deleted = []
            removed_dirs = []
            sync.delete_file = lambda path, remote: deleted.append((path, remote))
            sync.rmdir = lambda path, remote: removed_dirs.append((path, remote))
            sync.copy = lambda *_args: self.fail("matching source file should not be copied")

            sync.backup(source, delete_files=True, dry_run=False, target="local:" + target)

            self.assertEqual(deleted, [(target + "/removed.txt", "local:")])
            self.assertEqual(removed_dirs, [(target + "/orphan-dir", "local:")])

    def test_backup_without_delete_files_accepts_idless_shallow_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            os.makedirs(source)
            os.makedirs(target)
            with open(os.path.join(source, "keep.txt"), "w") as fp:
                fp.write("keep")

            sync = clsync.ClSync.__new__(clsync.ClSync)
            sync._show_progress = False
            sync._compare_method = "size"
            sync._ClSync__exclusion_list = None
            sync._ClSync__exclude_regex = None
            sync._config = {"no_cache": True, "ls_stop_first": False}
            sync._sa_registry = None
            sync._sa_refresh = "stale"
            sync._cache = {}
            sync._cache_counter = {}
            sync._cache_invalidation_max = 10
            payload = json.dumps([{
                "Path": "keep.txt",
                "Name": "keep.txt",
                "Size": 4,
                "MimeType": "text/plain",
                "ModTime": "2024-01-01T00:00:00Z",
                "IsDir": False,
            }])
            calls = []
            sync._rclone = types.SimpleNamespace(
                lsjson=lambda remote, path, args, _no_error: calls.append((remote, path, args)) or payload
            )
            sync.copy = lambda *_args: self.fail("matching source file should not be copied")
            sync.delete_file = lambda *_args: self.fail("delete_files=False must not delete files")
            sync.rmdir = lambda *_args: self.fail("delete_files=False must not delete directories")

            sync.backup(source, delete_files=False, dry_run=False, target="local:" + target)

            self.assertEqual(calls, [("local:", target, ["--fast-list"])])

    @unittest.skipUnless(shutil.which("rclone"), "rclone is required for the local backend integration test")
    def test_real_local_remote_backup_without_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            config_path = os.path.join(tmp, "rclone-test.conf")
            os.makedirs(source)
            os.makedirs(target)
            with open(config_path, "w") as fp:
                fp.write("[local_source]\ntype = local\n\n[local_target]\ntype = local\n")

            for name, content in (("keep.txt", "keep"), ("removed.txt", "remove me")):
                path = os.path.join(source, name)
                with open(path, "w") as fp:
                    fp.write(content)
                old_time = time.time() - (7 * 60 * 60)
                os.utime(path, (old_time, old_time))

            config = {
                "rclone_config": config_path,
                "distribution_type": "mas",
                "compare_method": "size",
                "rclone_retries": "1",
                "show_progress": False,
                "daemon_interval": 60,
                "no_cache": True,
                "ls_stop_first": False,
                "rclone_move": False,
            }
            sync = clsync.ClSync(config)
            explicit_target = "local_target:" + target

            sync.backup(source, delete_files=True, dry_run=False, target=explicit_target)
            self.assertTrue(os.path.isfile(os.path.join(target, "keep.txt")))
            self.assertTrue(os.path.isfile(os.path.join(target, "removed.txt")))

            os.remove(os.path.join(source, "removed.txt"))
            os.mkdir(os.path.join(target, "orphan-dir"))
            listing = json.loads(sync._rclone.lsjson(
                "local_target:", target, ["--recursive", "--fast-list"], True
            ))
            self.assertTrue(listing)
            self.assertTrue(all("ID" not in row for row in listing))

            sync.backup(source, delete_files=True, dry_run=False, target=explicit_target)
            self.assertFalse(os.path.exists(os.path.join(target, "removed.txt")))
            self.assertFalse(os.path.exists(os.path.join(target, "orphan-dir")))

            extra_path = os.path.join(target, "extra.txt")
            with open(extra_path, "w") as fp:
                fp.write("must remain")
            sync.backup(source, delete_files=False, dry_run=False, target=explicit_target)
            self.assertTrue(os.path.isfile(extra_path))

    def test_backup_without_delete_files_uses_shallow_remote_listings(self):
        with tempfile.TemporaryDirectory() as tmp:
            movie_dir = os.path.join(tmp, "Movies", "Aladin")
            os.makedirs(movie_dir)
            local_file = os.path.join(movie_dir, "movie.mkv")
            with open(local_file, "w") as fp:
                fp.write("synthetic movie")

            sync = clsync.ClSync.__new__(clsync.ClSync)
            sync._show_progress = False
            sync._compare_method = "size"
            sync._ClSync__exclusion_list = None
            sync._ClSync__exclude_regex = None
            sync.get_eligible_remotes = lambda _size: ["dst109:"]
            sync.mark_remote_used = lambda _remote, _size: None
            shallow_paths = []
            recursive_paths = []
            copies = []
            sync.ls_shallow = lambda path, **_kwargs: shallow_paths.append(path) or {}
            sync.ls = lambda path: recursive_paths.append(path) or {}
            sync.copy = lambda src, dst, remote: copies.append((src, dst, remote))

            old_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                sync.backup(movie_dir, delete_files=False, dry_run=False)
            finally:
                os.chdir(old_cwd)

            self.assertEqual(shallow_paths, ["/Movies/Aladin"])
            self.assertEqual(recursive_paths, [])
            self.assertEqual(copies, [(local_file, "/Movies/Aladin", "dst109:")])

    def test_backup_to_explicit_rclone_target_uses_target_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            movie_dir = os.path.join(tmp, "Manga")
            os.makedirs(movie_dir)
            local_file = os.path.join(movie_dir, "chapter.cbz")
            with open(local_file, "w") as fp:
                fp.write("synthetic manga")

            sync = clsync.ClSync.__new__(clsync.ClSync)
            sync._show_progress = False
            sync._compare_method = "size"
            sync._ClSync__exclusion_list = None
            sync._ClSync__exclude_regex = None
            sync.get_best_remote = lambda _size: self.fail("cluster placement should not be used")
            sync.mark_remote_used = lambda _remote, _size: self.fail("cluster quota should not be updated")
            shallow_calls = []
            copies = []
            sync.ls_shallow = lambda path, **kwargs: shallow_calls.append((
                path,
                kwargs.get("remotes"),
                kwargs.get("normalize_path"),
            )) or {}
            sync.copy = lambda src, dst, remote: copies.append((src, dst, remote))

            sync.backup(movie_dir, delete_files=False, dry_run=False, target="hidrive:public/Manga")

            self.assertEqual(shallow_calls, [("public/Manga", ["hidrive:"], False)])
            self.assertEqual(copies, [(local_file, "public/Manga", "hidrive:")])

    def test_backup_local_file_to_remote_root_does_not_delete_other_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            local_file = os.path.join(tmp, "movie.mkv")
            with open(local_file, "w") as fp:
                fp.write("synthetic movie")
            sync = clsync.ClSync.__new__(clsync.ClSync)
            sync._show_progress = False
            sync._compare_method = "size"
            sync._ClSync__exclusion_list = None
            sync._ClSync__exclude_regex = None
            sync._rclone = types.SimpleNamespace(mkdir=lambda *_args: None)
            sync.get_best_remote = lambda _size: self.fail("cluster placement should not be used")
            sync.mark_remote_used = lambda _remote, _size: self.fail("cluster quota should not be updated")
            shallow_calls = []
            copies = []
            sync.ls_shallow = lambda path, **kwargs: shallow_calls.append((path, kwargs["remotes"], kwargs["normalize_path"])) or {
                "/unrelated.mkv": types.SimpleNamespace(is_dir=False, path="/", name="unrelated.mkv", remote="hidrive:")
            }
            sync.copy = lambda src, dst, remote: copies.append((src, dst, remote))

            sync.backup(local_file, delete_files=True, dry_run=False, target="hidrive:")

            self.assertEqual(shallow_calls, [("/", ["hidrive:"], False)])
            self.assertEqual(copies, [(local_file, "/", "hidrive:")])

    def test_backup_directory_to_bare_remote_creates_basename_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "roms")
            os.mkdir(source)
            local_file = os.path.join(source, "game.zip")
            with open(local_file, "w") as fp:
                fp.write("synthetic game")
            sync = clsync.ClSync.__new__(clsync.ClSync)
            sync._show_progress = False
            sync._compare_method = "size"
            sync._ClSync__exclusion_list = None
            sync._ClSync__exclude_regex = None
            sync._rclone = types.SimpleNamespace(mkdir=lambda *_args: None)
            sync.get_best_remote = lambda _size: self.fail("cluster placement should not be used")
            sync.mark_remote_used = lambda _remote, _size: self.fail("cluster quota should not be updated")
            listed = []
            copies = []
            sync.ls = lambda path, **kwargs: listed.append((path, kwargs["remotes"], kwargs["normalize_path"])) or {}
            sync.copy = lambda src, dst, remote: copies.append((src, dst, remote))

            sync.backup(source, delete_files=True, dry_run=False, target="hidrive:")

            self.assertEqual(listed, [("/roms", ["hidrive:"], False)])
            self.assertEqual(copies, [(local_file, "/roms", "hidrive:")])

    def test_backup_from_rclone_source_to_rclone_target(self):
        sync = clsync.ClSync.__new__(clsync.ClSync)
        sync._show_progress = False
        sync._compare_method = "size"
        sync._ClSync__exclusion_list = None
        sync._ClSync__exclude_regex = None
        sync.get_best_remote = lambda _size: self.fail("cluster placement should not be used")
        sync.mark_remote_used = lambda _remote, _size: self.fail("cluster quota should not be updated")
        source_calls = []
        target_calls = []
        copies = []
        payload = json.dumps([{
            "Path": "chapter.cbz",
            "Name": "chapter.cbz",
            "Size": 10,
            "MimeType": "application/zip",
            "ModTime": "2024-01-01T00:00:00Z",
            "IsDir": False,
            "ID": "file-id",
        }])
        sync._rclone = types.SimpleNamespace(
            lsjson=lambda remote, path, args, _no_error: source_calls.append((remote, path, args)) or payload
        )
        sync.ls_shallow = lambda path, **kwargs: target_calls.append((
            path,
            kwargs.get("remotes"),
            kwargs.get("normalize_path"),
        )) or {}
        sync.copy = lambda src, dst, remote: copies.append((src, dst, remote))

        sync.backup("hidrive:public/Manga", delete_files=False, dry_run=False, target="backup:mirror/Manga")

        self.assertEqual(source_calls, [("hidrive:", "public/Manga", ["--recursive", "--fast-list"])])
        self.assertEqual(target_calls, [("mirror/Manga", ["backup:"], False)])
        self.assertEqual(copies, [("hidrive:public/Manga/chapter.cbz", "mirror/Manga", "backup:")])

    def test_rc_local_remote_maps_literal_source_path_to_server_remote(self):
        sync = clsync.ClSync.__new__(clsync.ClSync)
        sync._show_progress = False
        sync._compare_method = "size"
        sync._config = {"rclone_rc_local_remote": "mylocal"}
        sync._ClSync__exclusion_list = None
        sync._ClSync__exclude_regex = None
        sync.get_best_remote = lambda _size: "dst101:"
        sync.mark_remote_used = lambda _remote, _size: None
        source_calls = []
        sync._rclone = types.SimpleNamespace(
            _rc_url="https://rc.example.test",
            lsjson=lambda remote, path, args, _no_error: source_calls.append((remote, path, args)) or "[]",
        )
        sync.ls = lambda *_args, **_kwargs: {}

        sync.backup("/srv/media", delete_files=True, dry_run=True)

        self.assertEqual(source_calls, [("mylocal:", "/srv/media", ["--recursive", "--fast-list"])])

    def test_backup_from_rclone_source_to_cluster_uses_source_basename(self):
        sync = clsync.ClSync.__new__(clsync.ClSync)
        sync._show_progress = False
        sync._compare_method = "size"
        sync._ClSync__exclusion_list = None
        sync._ClSync__exclude_regex = None
        sync.get_eligible_remotes = lambda _size: ["dst109:"]
        sync.mark_remote_used = lambda _remote, _size: None
        source_calls = []
        target_calls = []
        copies = []
        payload = json.dumps([{
            "Path": "chapter.cbz",
            "Name": "chapter.cbz",
            "Size": 10,
            "MimeType": "application/zip",
            "ModTime": "2024-01-01T00:00:00Z",
            "IsDir": False,
            "ID": "file-id",
        }])
        sync._rclone = types.SimpleNamespace(
            lsjson=lambda remote, path, args, _no_error: source_calls.append((remote, path, args)) or payload
        )
        sync.ls_shallow = lambda path, **kwargs: target_calls.append((
            path,
            kwargs.get("remotes"),
            kwargs.get("normalize_path"),
        )) or {}
        sync.copy = lambda src, dst, remote: copies.append((src, dst, remote))

        sync.backup("hidrive:public/Manga", delete_files=False, dry_run=False)

        self.assertEqual(source_calls, [("hidrive:", "public/Manga", ["--recursive", "--fast-list"])])
        self.assertEqual(target_calls, [("/Manga", None, True)])
        self.assertEqual(copies, [("hidrive:public/Manga/chapter.cbz", "/Manga", "dst109:")])

    def test_parse_backup_target_preserves_rclone_path_style(self):
        sync = clsync.ClSync.__new__(clsync.ClSync)

        self.assertEqual(
            sync.parse_backup_target("hidrive:public/Manga"),
            ("hidrive:", "public/Manga"),
        )
        self.assertEqual(sync.parse_backup_target("hidrive:"), ("hidrive:", ""))
        self.assertEqual(
            sync.parse_backup_target("local:/private/tmp/Manga"),
            ("local:", "/private/tmp/Manga"),
        )
        self.assertEqual(sync.get_backup_remote_root_for_remote_source("hidrive:", "public/Manga"), "/Manga")

    def test_sa_stats_refreshes_quota_without_recursive_file_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            store = os.path.join(tmp, "store")
            db_path = os.path.join(tmp, "sa.sqlite3")
            os.mkdir(source)
            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))
            registry = service_accounts.ServiceAccountRegistry(db_path, store)
            registry.import_paths([source])
            old_quota = sprinkle._refresh_service_account_quota
            old_file_cache = sprinkle._refresh_service_account_file_cache
            old_print_line = common.print_line

            try:
                sprinkle._refresh_service_account_quota = (
                    lambda _account: ({"total": 100, "used": 20, "free": 80}, None)
                )
                sprinkle._refresh_service_account_file_cache = lambda _account: self.fail(
                    "sa-stats must not recursively list Drive files"
                )
                common.print_line = lambda _message="": None
                sprinkle.read_args([
                    "--sa-db",
                    db_path,
                    "--sa-store",
                    store,
                    "--sa-refresh",
                    "all",
                    "--rclone-env-file",
                    os.path.join(tmp, "rclone.env"),
                    "sa-stats",
                ])
                sprinkle.configure(None)
                sprinkle.sa_stats()
            finally:
                sprinkle._refresh_service_account_quota = old_quota
                sprinkle._refresh_service_account_file_cache = old_file_cache
                common.print_line = old_print_line

            cache_row = service_accounts.ServiceAccountRegistry(db_path, store).ls_cache_by_account_id(
                registry.active_accounts()[0]["id"],
                "/",
            )
            self.assertIsNone(cache_row)

    def test_sa_stats_hides_unknown_quota_account_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            store = os.path.join(tmp, "store")
            db_path = os.path.join(tmp, "sa.sqlite3")
            os.mkdir(source)
            write_json(os.path.join(source, "unknown.json"), make_service_account("unknown@example.test"))
            registry = service_accounts.ServiceAccountRegistry(db_path, store)
            registry.import_paths([source])
            account = registry.active_accounts()[0]
            registry.update_quota(account["id"], None, "rclone about returned unknown quota: missing free")
            messages = []
            old_print_line = common.print_line

            try:
                common.print_line = lambda message="": messages.append(message)
                sprinkle.read_args([
                    "--sa-db", db_path,
                    "--sa-store", store,
                    "--sa-refresh", "none",
                    "--rclone-env-file", os.path.join(tmp, "rclone.env"),
                    "sa-stats",
                ])
                sprinkle.configure(None)
                sprinkle.sa_stats()
            finally:
                common.print_line = old_print_line

            self.assertIn("unknown:     1", messages)
            self.assertFalse(any("unknown@example.test" in message for message in messages))

            registry.assume_unknown_quota_for_union(account["id"])
            registry.mark_union_unknown_accounts_full([account["id"]], "storage quota exceeded")
            messages = []
            try:
                common.print_line = lambda message="": messages.append(message)
                sprinkle.sa_stats()
            finally:
                common.print_line = old_print_line
            self.assertIn("unknown:     0", messages)
            self.assertIn("unknown-full:1", messages)
            self.assertFalse(any("unknown@example.test" in message for message in messages))

    def test_sa_stats_limits_parallel_refreshes_and_serializes_database_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            store = os.path.join(tmp, "store")
            db_path = os.path.join(tmp, "sa.sqlite3")
            os.mkdir(source)
            for index in range(5):
                write_json(
                    os.path.join(source, "{}.json".format(index)),
                    make_service_account("{}@example.test".format(index), "key-{}".format(index)),
                )
            registry = service_accounts.ServiceAccountRegistry(db_path, store)
            registry.import_paths([source])
            old_refresh = sprinkle._refresh_service_account_quota
            old_print_line = common.print_line
            active = [0]
            peak = [0]
            counter_lock = threading.Lock()

            def refresh(_account):
                with counter_lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.03)
                with counter_lock:
                    active[0] -= 1
                return {"total": 100, "used": 25, "free": 75}, None

            try:
                sprinkle._refresh_service_account_quota = refresh
                common.print_line = lambda _message="": None
                sprinkle.read_args([
                    "--sa-db", db_path,
                    "--sa-store", store,
                    "--sa-refresh", "all",
                    "--sa-stats-workers", "2",
                    "--rclone-env-file", os.path.join(tmp, "rclone.env"),
                    "sa-stats",
                ])
                sprinkle.configure(None)
                sprinkle.sa_stats()
            finally:
                sprinkle._refresh_service_account_quota = old_refresh
                common.print_line = old_print_line

            self.assertEqual(peak[0], 2)
            self.assertEqual(registry.all_account_stats()[0]["free"], 75)

    def test_rclone_rc_about_uses_basic_auth_without_local_fallback(self):
        previous_config = sprinkle.__dict__.get("__config")
        request_seen = []

        class Response(object):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"total": 100, "used": 20, "free": 80}'

        def urlopen(request, timeout):
            request_seen.append((request, timeout))
            return Response()

        try:
            sprinkle.__dict__["__config"] = {
                "rclone_rc_url": "https://rc.example.test/",
                "rclone_rc_user": "quota-user",
                "rclone_rc_password": "not-logged",
                "rclone_rc_timeout_seconds": 12,
            }
            with mock.patch.object(sprinkle.urllib_request, "urlopen", side_effect=urlopen):
                quota, error = sprinkle._rclone_rc_about("dst101:")
        finally:
            sprinkle.__dict__["__config"] = previous_config

        self.assertIsNone(error)
        self.assertEqual(quota["free"], 80)
        request, timeout = request_seen[0]
        self.assertEqual(request.full_url, "https://rc.example.test/operations/about")
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"fs": "dst101:"})
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))
        self.assertEqual(timeout, 12)

    def test_rc_sa_import_validator_stays_local_when_rc_is_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            store = os.path.join(tmp, "store")
            db_path = os.path.join(tmp, "sa.sqlite3")
            os.mkdir(source)
            existing_path = os.path.join(source, "existing.json")
            candidate_path = os.path.join(source, "candidate.json")
            write_json(existing_path, make_service_account("one@example.test"))
            candidate = make_service_account("two@example.test", "two-key")
            write_json(candidate_path, candidate)
            registry = service_accounts.ServiceAccountRegistry(db_path, store)
            registry.import_paths([existing_path])
            previous_config = sprinkle.__dict__.get("__config")
            try:
                sprinkle.__dict__["__config"] = {"rclone_rc_url": "https://rc.example.test"}
                with mock.patch.object(
                        sprinkle.rclone.RClone, 'get_about_json_with_error',
                        return_value=({"total": 100, "free": 80}, None)) as about:
                    quota, error = sprinkle._service_account_live_validator(candidate_path, candidate, registry)
            finally:
                sprinkle.__dict__["__config"] = previous_config

            self.assertIsNone(error)
            self.assertEqual(quota["free"], 80)
            self.assertEqual(about.call_args[0][0], "sa_import1:")

    def test_import_validation_workers_run_candidates_concurrently(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, 'source')
            os.mkdir(source)
            for index in range(3):
                write_json(os.path.join(source, '{}.json'.format(index)), make_service_account(
                    'worker{}@example.test'.format(index), 'worker-key-{}'.format(index)
                ))
            registry = service_accounts.ServiceAccountRegistry(
                os.path.join(tmp, 'sa.sqlite3'), os.path.join(tmp, 'store')
            )
            active = [0]
            peak = [0]
            lock = threading.Lock()

            def validator(_path, _payload):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.05)
                with lock:
                    active[0] -= 1
                return {"total": 100, "free": 80}, None

            result = registry.import_paths([source], validator=validator, validation_workers=3)
            self.assertEqual(result.imported, 3)
            self.assertGreaterEqual(peak[0], 2)

    def test_rc_drive_slot_update_keeps_credentials_out_of_logs(self):
        rc = rclone.RClone(rc_url='http://rc.example.test')
        calls = []
        rc._rc_call = lambda endpoint, payload: calls.append((endpoint, payload)) or {}
        credential = make_service_account('slot@example.test')
        rc.configure_rc_drive_service_account('dst102:', credential)
        endpoint, payload = calls[0]
        self.assertEqual(endpoint, 'config/update')
        self.assertEqual(payload['name'], 'dst102')
        self.assertEqual(json.loads(payload['parameters']['service_account_credentials'])['client_email'], 'slot@example.test')
        self.assertNotIn('private_key', str({'name': payload['name'], 'opt': payload['opt']}))

    def test_rclone_transport_routes_operations_through_rc(self):
        calls = []

        class Response(object):
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        def urlopen(request, timeout):
            endpoint = request.full_url.rsplit("/", 1)[-1]
            payload = json.loads(request.data.decode("utf-8"))
            calls.append((endpoint, payload, timeout))
            if endpoint == "listremotes":
                return Response({"remotes": ["dst101"]})
            if endpoint == "list":
                return Response({"list": [{"Name": "movie.mkv", "Size": 10, "IsDir": False}]})
            return Response({})

        rc = rclone.RClone(
            rc_url="https://rc.example.test",
            rc_user="user",
            rc_password="not-logged",
            rc_timeout_seconds=9,
        )
        with mock.patch.object(rclone.urllib_request, "urlopen", side_effect=urlopen):
            self.assertEqual(rc.get_remotes(), ["dst101:"])
            self.assertEqual(json.loads(rc.lsjson("dst101:", "/movies", ["--recursive"])), [
                {"Name": "movie.mkv", "Size": 10, "IsDir": False}
            ])
            rc.copy("/srv/source/movie.mkv", "dst101:/movies")
            rc.move("/srv/source/other.mkv", "dst101:/movies")
            rc.delete_file("dst101:", "/movies/movie.mkv")

        self.assertEqual(calls[0][0], "listremotes")
        self.assertEqual(calls[1][0], "list")
        self.assertEqual(calls[1][1], {
            "fs": "dst101:",
            "remote": "movies",
            "opt": {"recurse": True, "showOrigIDs": True},
        })
        self.assertEqual(calls[2], ("copyfile", {
            "srcFs": "/srv/source", "srcRemote": "movie.mkv",
            "dstFs": "dst101:/movies", "dstRemote": "movie.mkv",
        }, 9))
        self.assertEqual(calls[3], ("movefile", {
            "srcFs": "/srv/source", "srcRemote": "other.mkv",
            "dstFs": "dst101:/movies", "dstRemote": "other.mkv",
        }, 9))
        self.assertEqual(calls[4][0], "deletefile")

    def test_rc_cluster_operations_apply_configured_drive_root_only_to_destinations(self):
        calls = []
        rc = rclone.RClone(
            rc_url="https://rc.example.test",
            rc_drive_id="drive-folder-id",
            rc_drive_remotes=["dst101:", "dst102:"],
        )
        def rc_call(endpoint, payload=None):
            calls.append((endpoint, payload))
            if endpoint == "operations/about":
                return {"total": 100, "free": 80}
            return {"list": []}

        rc._rc_call = rc_call

        rc.lsjson("dst101:", "/roms", ["--recursive"])
        rc.mkdir("dst101:", "/roms/GG")
        rc.copy("mylocal:/shared/downloads/game.gg", "dst101:/roms/GG")
        rc.delete_file("dst101:", "/roms/GG/game.gg")
        rc.get_about_json("dst101:")

        self.assertEqual(calls[0][1]["fs"], "dst101,root_folder_id=drive-folder-id:")
        self.assertEqual(calls[1][1]["fs"], "dst101,root_folder_id=drive-folder-id:")
        self.assertEqual(calls[2], ("operations/copyfile", {
            "srcFs": "mylocal:/shared/downloads",
            "srcRemote": "game.gg",
            "dstFs": "dst101,root_folder_id=drive-folder-id:/roms/GG",
            "dstRemote": "game.gg",
        }))
        self.assertEqual(calls[3][1]["fs"], "dst101,root_folder_id=drive-folder-id:")
        self.assertEqual(calls[4][1]["fs"], "dst101:")

    def test_rclone_rc_lsjson_normalizes_paths_to_requested_directory(self):
        class Response(object):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"list": [{
                    "Path": "shared/downloads/roms/GBA/game.zip",
                    "Name": "game.zip",
                    "Size": 10,
                    "IsDir": False,
                }]}).encode("utf-8")

        rc = rclone.RClone(rc_url="https://rc.example.test")
        with mock.patch.object(rclone.urllib_request, "urlopen", return_value=Response()):
            rows = json.loads(rc.lsjson("mylocal:", "/shared/downloads/roms", ["--recursive"], True))

        self.assertEqual(rows[0]["Path"], "GBA/game.zip")

    def test_config_log_redacts_rc_and_smtp_passwords(self):
        logged = sprinkle._config_for_log({
            "rclone_rc_password": "rc-secret",
            "smtp_password": "smtp-secret",
        })
        self.assertEqual(logged["rclone_rc_password"], "<redacted>")
        self.assertEqual(logged["smtp_password"], "<redacted>")

    def test_rc_refresh_uses_local_validation_for_an_unbound_account(self):
        previous_config = sprinkle.__dict__.get("__config")
        try:
            sprinkle.__dict__["__config"] = {
                "rclone_rc_url": "https://rc.example.test",
                "rclone_rc_timeout_seconds": 30,
            }
            with mock.patch.object(sprinkle, '_refresh_service_account_file_cache', return_value=({"total": 100, "free": 80}, None)) as refresh:
                quota, error = sprinkle._refresh_service_account_quota({
                    "remote_name": None,
                    "managed_path": "/does/not/matter.json",
                })
        finally:
            sprinkle.__dict__["__config"] = previous_config

        self.assertEqual(quota["free"], 80)
        self.assertIsNone(error)
        refresh.assert_called_once()

    def test_unbound_rc_quota_refresh_uses_about_not_lsjson(self):
        with tempfile.TemporaryDirectory() as tmp:
            credential = os.path.join(tmp, 'account.json')
            write_json(credential, make_service_account('quota@example.test'))
            previous_config = sprinkle.__dict__.get('__config')
            fake = mock.Mock()
            fake.get_about_json_with_error.return_value = ({'total': 100, 'free': 80}, None)
            try:
                sprinkle.__dict__['__config'] = {'drive_id': 'drive-id', 'rclone_exe': 'rclone', 'rclone_retries': '1'}
                with mock.patch.object(rclone, 'RClone', return_value=fake):
                    quota, error = sprinkle._refresh_service_account_file_cache({
                        'managed_path': credential, 'client_email': 'quota@example.test', 'project_id': 'project',
                    })
            finally:
                sprinkle.__dict__['__config'] = previous_config
            self.assertEqual(quota['free'], 80)
            self.assertIsNone(error)
            fake.get_about_json_with_error.assert_called_once_with('sa_files1:')
            self.assertFalse(hasattr(fake, 'lsjson') and fake.lsjson.called)

    def test_rc_remotes_skip_local_service_account_config_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            sprinkle.read_args([
                "--rclone-rc-url", "https://rc.example.test",
                "--rclone-rc-remotes", "dst101,dst102:",
                "--drive-id", "drive-id",
                "--rclone-env-file", os.path.join(tmp, "rclone.env"),
                "backup", "/server-visible/source",
            ])
            sprinkle.configure(None)
            sprinkle.prepare_rclone_sa_config()

            self.assertEqual(
                sprinkle.__dict__["__config"]["cluster_remotes"],
                ["dst101:", "dst102:"],
            )
            self.assertIsNone(sprinkle.__dict__["__config"]["rclone_config"])

    def test_rc_cluster_remotes_require_drive_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            sprinkle.read_args([
                "--rclone-rc-url", "https://rc.example.test",
                "--rclone-rc-remotes", "dst101",
                "--rclone-env-file", os.path.join(tmp, "rclone.env"),
                "backup", "/server-visible/source",
            ])
            sprinkle.configure(None)

            with self.assertRaisesRegex(Exception, "requires --drive-id"):
                sprinkle.prepare_rclone_sa_config()

    def test_rc_http_500_cleanup_option_is_parsed(self):
        sprinkle.read_args(["--sa-delete-rc-http-500", "sa-stats"])
        sprinkle.configure(None)

        self.assertTrue(sprinkle.__dict__["__config"]["sa_delete_rc_http_500"])

    def test_backup_preserves_cwd_relative_directory_in_remote_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            movie_dir = os.path.join(tmp, "Movies", "Aladin")
            os.makedirs(movie_dir)
            local_file = os.path.join(movie_dir, "movie.mkv")
            with open(local_file, "w") as fp:
                fp.write("synthetic movie")
            old_cwd = os.getcwd()
            sync = clsync.ClSync.__new__(clsync.ClSync)
            sync._show_progress = False
            sync._compare_method = "size"
            sync._ClSync__exclusion_list = None
            sync._ClSync__exclude_regex = None
            sync.get_eligible_remotes = lambda _size: ["dst109:"]
            sync.mark_remote_used = lambda _remote, _size: None
            listed_paths = []
            copies = []
            sync.ls_shallow = lambda path, **_kwargs: listed_paths.append(path) or {}
            sync.copy = lambda src, dst, remote: copies.append((src, dst, remote))

            try:
                os.chdir(tmp)
                sync.backup(movie_dir, delete_files=False, dry_run=False)
            finally:
                os.chdir(old_cwd)

            self.assertEqual(listed_paths, ["/Movies/Aladin"])
            self.assertEqual(copies, [(local_file, "/Movies/Aladin", "dst109:")])

    def test_large_file_selection_requires_headroom(self):
        sync = clsync.ClSync.__new__(clsync.ClSync)
        sync._distribution_type = "mas"
        sync._cached_free = {}
        sync._large_file_threshold_bytes = 1024
        sync._large_file_min_free_bytes = 100
        sync._large_file_min_free_percent = 10
        sync.get_remotes = lambda: ["tight:", "roomy:"]
        quotas = {
            "tight:": {"free": 1099},
            "roomy:": {"free": 2000},
        }
        sync._get_remote_quota = lambda remote: quotas[remote]

        self.assertEqual(sync.get_best_remote(1000), "roomy:")

    def test_small_file_selection_keeps_existing_most_free_behavior(self):
        sync = clsync.ClSync.__new__(clsync.ClSync)
        sync._distribution_type = "mas"
        sync._cached_free = {}
        sync._large_file_threshold_bytes = 1024
        sync._large_file_min_free_bytes = 100
        sync._large_file_min_free_percent = 10
        sync.get_remotes = lambda: ["one:", "two:"]
        quotas = {
            "one:": {"free": 500},
            "two:": {"free": 700},
        }
        sync._get_remote_quota = lambda remote: quotas[remote]

        self.assertEqual(sync.get_best_remote(100), "two:")

    def test_existing_update_remote_must_have_headroom(self):
        sync = clsync.ClSync.__new__(clsync.ClSync)
        sync._distribution_type = "mas"
        sync._cached_free = {}
        sync._large_file_threshold_bytes = 1024
        sync._large_file_min_free_bytes = 100
        sync._large_file_min_free_percent = 10
        sync._get_remote_quota = lambda _remote: {"free": 1099}

        with self.assertRaises(Exception):
            sync.ensure_remote_has_enough_space("tight:", 1024)

    def test_unknown_quota_is_not_cached_and_is_retried(self):
        sync = clsync.ClSync.__new__(clsync.ClSync)
        sync._cached_free = {}
        calls = []

        def quota(_remote):
            calls.append(True)
            if len(calls) == 1:
                return None
            return {"free": 100}

        sync._get_remote_quota = quota

        self.assertIsNone(sync._known_free_for_remote("dst101:"))
        self.assertEqual(sync._known_free_for_remote("dst101:"), 100)
        self.assertEqual(len(calls), 2)

    def test_generic_transfer_failure_excludes_remote_only_for_current_run(self):
        sync = clsync.ClSync.__new__(clsync.ClSync)
        sync._distribution_type = "mas"
        sync._cached_free = {"dst102:": 100, "dst101:": 100}
        sync._run_unavailable_remotes = {}
        sync._large_file_threshold_bytes = 1024
        sync._large_file_min_free_bytes = 100
        sync._large_file_min_free_percent = 10
        sync.get_remotes = lambda: ["dst102:", "dst101:"]

        sync.mark_remote_unavailable_for_run("dst102:", "rclone RC sync/move failed: HTTP 500")

        self.assertEqual(sync.get_eligible_remotes(1), ["dst101:"])
        self.assertEqual(sync._cached_free["dst102:"], 100)

    def test_quota_exhaustion_excludes_remote_for_the_remainder_of_the_run(self):
        sync = clsync.ClSync.__new__(clsync.ClSync)
        sync._distribution_type = "mas"
        sync._cached_free = {"dst102:": 100, "dst101:": 100}
        sync._run_unavailable_remotes = {}
        sync._run_quota_exhausted_remotes = set()
        sync._frees = None
        sync._sa_registry = None
        sync._large_file_threshold_bytes = 1024
        sync._large_file_min_free_bytes = 100
        sync._large_file_min_free_percent = 10
        sync.get_remotes = lambda: ["dst102:", "dst101:"]

        sync.mark_remote_quota_exhausted("dst101:")

        self.assertEqual(sync.get_eligible_remotes(1), ["dst102:"])
        self.assertEqual(sync._cached_free["dst101:"], 0)

    def test_quota_query_failure_is_attempted_once_per_backup_run(self):
        sync = clsync.ClSync.__new__(clsync.ClSync)
        sync._distribution_type = "mas"
        sync._cached_free = {}
        sync._run_unavailable_remotes = {}
        sync._run_quota_exhausted_remotes = set()
        sync._run_quota_error_remotes = {}
        sync._frees = None
        sync._sa_registry = None
        sync._sa_refresh = "stale"
        sync._large_file_threshold_bytes = 1024
        sync._large_file_min_free_bytes = 100
        sync._large_file_min_free_percent = 10
        sync.get_remotes = lambda: ["dst102:"]
        calls = []
        sync._rclone = types.SimpleNamespace(
            get_about_json_with_error=lambda remote: calls.append(remote) or (None, "RC timeout")
        )

        self.assertEqual(sync.get_eligible_remotes(1), [])
        self.assertEqual(sync.get_eligible_remotes(1), [])
        self.assertEqual(calls, ["dst102:"])
        self.assertNotIn("dst102:", sync._cached_free)

    def test_backup_progress_escapes_percent_in_filename(self):
        class FormattingBar(object):
            def __init__(self, *_args, **_kwargs):
                self.message = ""

            def next(self):
                self.message % {"index": 1, "max": 1, "percent": 100, "elapsed_td": "0", "eta_td": "0"}

            def finish(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            local_file = os.path.join(tmp, "game-100%_complete.gg")
            with open(local_file, "w") as fp:
                fp.write("synthetic game")
            sync = clsync.ClSync.__new__(clsync.ClSync)
            sync._show_progress = True
            sync._compare_method = "size"
            sync._ClSync__exclusion_list = None
            sync._ClSync__exclude_regex = None
            sync.ls_shallow = lambda _path, **_kwargs: {}
            sync.get_eligible_remotes = lambda _size: ["dst101:"]
            sync.copy = lambda *_args: None
            sync.mark_remote_used = lambda *_args: None

            with mock.patch.object(clsync, "Bar", FormattingBar):
                sync.backup(tmp, delete_files=False, dry_run=False)

    def test_backup_retries_add_on_next_eligible_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            local_file = os.path.join(tmp, "movie.mkv")
            with open(local_file, "w") as fp:
                fp.write("synthetic movie")
            sync = clsync.ClSync.__new__(clsync.ClSync)
            sync._show_progress = False
            sync._compare_method = "size"
            sync._ClSync__exclusion_list = None
            sync._ClSync__exclude_regex = None
            sync.ls_shallow = lambda _path, **_kwargs: {}
            sync.get_eligible_remotes = lambda _size: ["dst102:", "dst101:"]
            copies = []
            marked = []

            def copy(src, dst, remote):
                copies.append((src, dst, remote))
                if remote == "dst102:":
                    raise Exception("temporary remote failure")

            sync.copy = copy
            sync.mark_remote_used = lambda remote, size: marked.append((remote, size))

            sync.backup(tmp, delete_files=False, dry_run=False)

            self.assertEqual([call[2] for call in copies], ["dst102:", "dst101:"])
            self.assertEqual(marked, [("dst101:", len("synthetic movie"))])

    def test_backup_creates_destination_directory_before_each_add_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            local_file = os.path.join(tmp, "movie.mkv")
            with open(local_file, "w") as fp:
                fp.write("synthetic movie")
            sync = clsync.ClSync.__new__(clsync.ClSync)
            sync._show_progress = False
            sync._compare_method = "size"
            sync._ClSync__exclusion_list = None
            sync._ClSync__exclude_regex = None
            sync._rclone = types.SimpleNamespace(mkdir=mock.Mock())
            sync.ls_shallow = lambda _path, **_kwargs: {}
            sync.get_eligible_remotes = lambda _size: ["dst101:"]
            sync.copy = lambda *_args: (_ for _ in ()).throw(Exception("transfer failed"))

            with self.assertRaisesRegex(Exception, "transfer failed"):
                sync.backup(tmp, delete_files=False, dry_run=False)

            sync._rclone.mkdir.assert_called_once_with("dst101:", "/" + os.path.basename(tmp))

    def test_rclone_move_accepts_successful_stderr_progress_output(self):
        rc = rclone.RClone()
        with mock.patch.object(rclone.common, "execute", return_value={
            "code": 0,
            "out": "",
            "error": "INFO: movie.mkv: Copied (new)\\nINFO: movie.mkv: Deleted\\n",
        }):
            self.assertEqual(rc.move("/source/movie.mkv", "dst101:/movies"), [])

    def test_backup_marks_storage_quota_remote_full_before_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            local_file = os.path.join(tmp, "movie.mkv")
            with open(local_file, "w") as fp:
                fp.write("synthetic movie")
            sync = clsync.ClSync.__new__(clsync.ClSync)
            sync._show_progress = False
            sync._compare_method = "size"
            sync._ClSync__exclusion_list = None
            sync._ClSync__exclude_regex = None
            sync._cached_free = {"dst102:": 100, "dst101:": 100}
            sync._frees = None
            sync._sa_registry = None
            sync.ls_shallow = lambda _path, **_kwargs: {}
            sync.get_eligible_remotes = lambda _size: ["dst102:", "dst101:"]
            copied = []

            def copy(_src, _dst, remote):
                copied.append(remote)
                if remote == "dst102:":
                    raise Exception("rclone RC sync/move failed: HTTP 500: googleapi: Error 403, storageQuotaExceeded")

            sync.copy = copy
            sync.mark_remote_used = lambda _remote, _size: None

            sync.backup(tmp, delete_files=False, dry_run=False)

            self.assertEqual(copied, ["dst102:", "dst101:"])
            self.assertEqual(sync._cached_free["dst102:"], 0)

    def test_backup_uses_two_parallel_directory_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            for directory in ("first", "second"):
                path = os.path.join(tmp, directory)
                os.mkdir(path)
                with open(os.path.join(path, "rom.bin"), "wb") as fp:
                    fp.write(b"synthetic rom")
            sync = clsync.ClSync.__new__(clsync.ClSync)
            sync._show_progress = False
            sync._compare_method = "size"
            sync._ClSync__exclusion_list = None
            sync._ClSync__exclude_regex = None
            sync._distribution_type = "mas"
            sync._cached_free = {"dst101:": 1000000}
            sync._frees = None
            sync._sa_registry = None
            sync._run_quota_exhausted_remotes = set()
            sync._run_unavailable_remotes = {}
            sync._backup_transfer_workers = 2
            sync._backup_transfer_lock = threading.Lock()
            sync._reserved_free = {}
            sync._large_file_threshold_bytes = clsync.DEFAULT_LARGE_FILE_THRESHOLD_BYTES
            sync._large_file_min_free_bytes = clsync.DEFAULT_LARGE_FILE_MIN_FREE_BYTES
            sync._large_file_min_free_percent = clsync.DEFAULT_LARGE_FILE_MIN_FREE_PERCENT
            sync.ls_shallow = lambda _path, **_kwargs: {}
            sync.get_eligible_remotes = lambda _size: ["dst101:"]
            sync._ensure_target_directory = lambda *_args: None
            sync.mark_remote_used = lambda *_args: None
            active = [0]
            peak = [0]
            active_lock = threading.Lock()

            def copy(*_args):
                with active_lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.04)
                with active_lock:
                    active[0] -= 1

            sync.copy = copy
            sync.backup(tmp, delete_files=False, dry_run=False)
            self.assertEqual(peak[0], 2)

    def test_backup_continues_when_all_add_candidates_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "first.mkv")
            second = os.path.join(tmp, "second.mkv")
            for path in (first, second):
                with open(path, "w") as fp:
                    fp.write("synthetic movie")
            sync = clsync.ClSync.__new__(clsync.ClSync)
            sync._show_progress = False
            sync._compare_method = "size"
            sync._ClSync__exclusion_list = None
            sync._ClSync__exclude_regex = None
            sync.ls_shallow = lambda _path, **_kwargs: {}
            sync.get_eligible_remotes = lambda _size: ["dst102:", "dst101:"]
            copied = []
            marked = []

            def copy(src, _dst, remote):
                copied.append((os.path.basename(src), remote))
                if os.path.basename(src) == "first.mkv":
                    raise Exception("both remotes unavailable")

            sync.copy = copy
            sync.mark_remote_used = lambda remote, _size: marked.append(remote)

            with self.assertRaisesRegex(Exception, "1 failed operation"):
                sync.backup(tmp, delete_files=False, dry_run=False)

            self.assertEqual(
                copied,
                [("first.mkv", "dst102:"), ("first.mkv", "dst101:"), ("second.mkv", "dst102:")],
            )
            self.assertEqual(marked, ["dst102:"])

    def test_backup_continues_after_update_and_delete_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync = clsync.ClSync.__new__(clsync.ClSync)
            sync._show_progress = False
            sync._compare_method = "size"
            sync._ClSync__exclusion_list = None
            sync._ClSync__exclude_regex = None
            sync.index_local_dir = lambda _path, _exclusions: {}
            sync.ls = lambda _path, **_kwargs: {}
            update = types.SimpleNamespace(
                operation="update",
                src=types.SimpleNamespace(
                    path=tmp, name="update.mkv", size=10, remote="dst101:", remote_path="/",
                    is_dir=False,
                ),
            )
            remove = types.SimpleNamespace(
                operation="remove",
                src=types.SimpleNamespace(
                    path="/old.mkv", name="old.mkv", size=0, remote="dst101:", remote_path="/",
                    is_dir=False,
                ),
            )
            sync.compare_clfiles_for_remote_root = lambda *_args: [update, remove]
            sync.ensure_remote_has_enough_space = lambda _remote, _size: None
            sync.copy = lambda *_args: (_ for _ in ()).throw(Exception("update failed"))
            deleted = []
            sync.delete_file = lambda path, remote: deleted.append((path, remote)) or (_ for _ in ()).throw(
                Exception("delete failed")
            )

            with self.assertRaisesRegex(Exception, "2 failed operation"):
                sync.backup(tmp, delete_files=True, dry_run=False)

            self.assertEqual(deleted, [("/old.mkv", "dst101:")])


class ServiceAccountCliTest(unittest.TestCase):
    def test_sprinkle_service_account_target_is_not_treated_as_external_remote(self):
        self.assertTrue(sprinkle._is_sprinkle_service_account_target("dst101:/"))
        self.assertTrue(sprinkle._is_sprinkle_service_account_target("dst136:/roms"))
        self.assertFalse(sprinkle._is_sprinkle_service_account_target("hidrive:/roms"))

    def test_service_account_about_uses_and_removes_generated_config(self):
        old_execute = common.execute
        old_config = getattr(sprinkle, "__config", None)
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            account_path = os.path.join(tmp, "account.json")
            write_json(account_path, make_service_account("one@example.test"))

            def fake_execute(command, no_error=False):
                config_index = command.index("--config")
                generated = command[config_index + 1]
                self.assertTrue(os.path.isfile(generated))
                calls.append((list(command), generated))
                return {
                    "code": 0,
                    "out": json.dumps({"total": 100, "used": 25, "free": 75}),
                    "error": "",
                }

            try:
                common.execute = fake_execute
                setattr(sprinkle, "__config", {
                    "drive_id": "drive-id",
                    "rclone_retries": "1",
                })
                quota, error = sprinkle._refresh_service_account_quota({
                    "managed_path": account_path,
                    "client_email": "one@example.test",
                    "project_id": "synthetic-project",
                })
            finally:
                common.execute = old_execute
                setattr(sprinkle, "__config", old_config)

            self.assertIsNone(error)
            self.assertEqual(quota["free"], 75)
            self.assertEqual(calls[0][0][1], "about")
            self.assertFalse(os.path.exists(calls[0][1]))

    def test_rclone_subprocess_does_not_inherit_rclone_config(self):
        old_config = os.environ.get("RCLONE_CONFIG")
        try:
            os.environ["RCLONE_CONFIG"] = "/production/old-rclone.conf"
            process = mock.MagicMock()
            process.__enter__.return_value = process
            process.communicate.return_value = (b"", b"")
            process.returncode = 0

            with mock.patch.object(common.subprocess, "Popen", return_value=process) as popen:
                rclone.RClone().get_version()

            child_env = popen.call_args.kwargs["env"]
            self.assertNotIn("RCLONE_CONFIG", child_env)
            self.assertEqual(os.environ["RCLONE_CONFIG"], "/production/old-rclone.conf")
        finally:
            if old_config is None:
                os.environ.pop("RCLONE_CONFIG", None)
            else:
                os.environ["RCLONE_CONFIG"] = old_config

    def test_rclone_env_file_rolls_out_and_loads_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, "rclone.env")
            keys = [key for key, _value in sprinkle.DEFAULT_RCLONE_ENV_VALUES]
            old_env = dict((key, os.environ.get(key)) for key in keys)
            try:
                for key in keys:
                    os.environ.pop(key, None)

                loaded = sprinkle.apply_rclone_env_file(env_path)

                self.assertTrue(os.path.exists(env_path))
                with open(env_path) as fp:
                    content = fp.read()
                self.assertIn("# Lines whose first non-space character is # are ignored.", content)
                self.assertEqual(loaded["RCLONE_DRIVE_CHUNK_SIZE"], "256M")
                self.assertEqual(os.environ["RCLONE_SIZE_ONLY"], "1")
                self.assertEqual(os.environ["RCLONE_NO_UPDATE_MODTIME"], "1")
            finally:
                for key, value in old_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_rclone_env_file_ignores_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, "rclone.env")
            with open(env_path, "w") as fp:
                fp.write("\n".join([
                    "# RCLONE_SIZE_ONLY=0",
                    "  # RCLONE_NO_UPDATE_MODTIME=0",
                    "RCLONE_DRIVE_CHUNK_SIZE=512M",
                    "RCLONE_EXTRA=value=with=equals",
                    "RCLONE_CONFIG=/must/not/be/used.conf",
                    "",
                ]))
            keys = [
                "RCLONE_SIZE_ONLY",
                "RCLONE_NO_UPDATE_MODTIME",
                "RCLONE_DRIVE_CHUNK_SIZE",
                "RCLONE_EXTRA",
                "RCLONE_CONFIG",
            ]
            old_env = dict((key, os.environ.get(key)) for key in keys)
            try:
                for key in keys:
                    os.environ.pop(key, None)

                loaded = sprinkle.apply_rclone_env_file(env_path)

                self.assertNotIn("RCLONE_SIZE_ONLY", loaded)
                self.assertNotIn("RCLONE_NO_UPDATE_MODTIME", loaded)
                self.assertEqual(os.environ["RCLONE_DRIVE_CHUNK_SIZE"], "512M")
                self.assertEqual(os.environ["RCLONE_EXTRA"], "value=with=equals")
                self.assertNotIn("RCLONE_CONFIG", loaded)
                self.assertNotIn("RCLONE_CONFIG", os.environ)
            finally:
                for key, value in old_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_configure_rolls_out_default_rclone_env_file(self):
        old_home = os.environ.get("HOME")
        keys = [key for key, _value in sprinkle.DEFAULT_RCLONE_ENV_VALUES]
        old_env = dict((key, os.environ.get(key)) for key in keys)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.environ["HOME"] = tmp
                for key in keys:
                    os.environ.pop(key, None)

                sprinkle.read_args(["stats"])
                sprinkle.configure(None)

                env_path = os.path.join(tmp, ".sprinkle", "rclone.env")
                self.assertTrue(os.path.exists(env_path))
                self.assertEqual(os.environ["RCLONE_DRIVE_CHUNK_SIZE"], "256M")
                self.assertEqual(getattr(sprinkle, "__config")["rclone_env_file"], env_path)
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
                for key, value in old_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_rc_password_can_come_from_environment_without_a_config_secret(self):
        previous = os.environ.get("SPRINKLE_RCLONE_RC_PASSWORD")
        try:
            os.environ["SPRINKLE_RCLONE_RC_PASSWORD"] = "runtime-only-password"
            sprinkle.read_args(["stats"])
            sprinkle.configure(None)
            self.assertEqual(getattr(sprinkle, "__config")["rclone_rc_password"], "runtime-only-password")
        finally:
            if previous is None:
                os.environ.pop("SPRINKLE_RCLONE_RC_PASSWORD", None)
            else:
                os.environ["SPRINKLE_RCLONE_RC_PASSWORD"] = previous

    def test_dash_v_sets_rclone_verbose(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_verbose = os.environ.get("RCLONE_VERBOSE")
            try:
                os.environ.pop("RCLONE_VERBOSE", None)
                sprinkle.read_args([
                    "-v",
                    "--rclone-env-file",
                    os.path.join(tmp, "rclone.env"),
                    "stats",
                ])
                sprinkle.configure(None)

                self.assertEqual(os.environ["RCLONE_VERBOSE"], "1")
            finally:
                if old_verbose is None:
                    os.environ.pop("RCLONE_VERBOSE", None)
                else:
                    os.environ["RCLONE_VERBOSE"] = old_verbose

    def test_progress_option_sets_show_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            sprinkle.read_args([
                "--progress",
                "--rclone-env-file",
                os.path.join(tmp, "rclone.env"),
                "backup",
                "/tmp/local",
            ])
            sprinkle.configure(None)

            self.assertTrue(getattr(sprinkle, "__config")["show_progress"])

    def test_rclone_sa_dir_imports_deduped_managed_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            store = os.path.join(tmp, "store")
            db_path = os.path.join(tmp, "sa.sqlite3")
            os.mkdir(source)

            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))
            write_json(os.path.join(source, "dupe.json"), make_service_account("one@example.test"))

            sprinkle.read_args([
                "--rclone-sa-dir",
                source,
                "--rclone-sa-count",
                "1",
                "--drive-id",
                "drive-id",
                "--rclone-env-file",
                os.path.join(tmp, "rclone.env"),
                "--sa-db",
                db_path,
                "--sa-store",
                store,
                "--rclone-conf",
                os.path.join(tmp, "empty-rclone.conf"),
                "stats",
            ])
            sprinkle.configure(None)
            sprinkle.prepare_rclone_sa_config()
            conf_path = getattr(sprinkle, "__rclone_conf")
            self.assertTrue(os.path.exists(conf_path))
            with open(conf_path) as fp:
                content = fp.read()
            self.assertEqual(content.count("[dst"), 1)
            self.assertIn("root_folder_id = drive-id", content)
            self.assertIn(os.path.abspath(store), content)
            os.unlink(conf_path)

    def test_service_account_config_includes_existing_rclone_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            store = os.path.join(tmp, "store")
            db_path = os.path.join(tmp, "sa.sqlite3")
            base_conf = os.path.join(tmp, "rclone.conf")
            os.mkdir(source)
            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))
            with open(base_conf, "w") as fp:
                fp.write("[hidrive]\ntype = local\n")

            sprinkle.read_args([
                "--rclone-conf",
                base_conf,
                "--rclone-sa-dir",
                source,
                "--rclone-sa-count",
                "1",
                "--drive-id",
                "drive-id",
                "--rclone-env-file",
                os.path.join(tmp, "rclone.env"),
                "--sa-db",
                db_path,
                "--sa-store",
                store,
                "stats",
            ])
            sprinkle.configure(None)
            sprinkle.prepare_rclone_sa_config()
            conf_path = getattr(sprinkle, "__rclone_conf")
            with open(conf_path) as fp:
                content = fp.read()

            self.assertIn("[hidrive]", content)
            self.assertIn("[dst101]", content)
            self.assertEqual(getattr(sprinkle, "__config")["cluster_remotes"], ["dst101:"])
            os.unlink(conf_path)

    def test_explicit_backup_target_skips_default_service_account_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            sprinkle.read_args([
                "--rclone-sa-dir",
                os.path.join(tmp, "accounts"),
                "--rclone-env-file",
                os.path.join(tmp, "rclone.env"),
                "backup",
                "/tmp/local",
                "hidrive:public/Manga",
            ])
            sprinkle.configure(None)
            sprinkle.prepare_rclone_sa_config()

            self.assertNotIn("rclone_config", getattr(sprinkle, "__config"))
            self.assertEqual(getattr(sprinkle, "__config")["rclone_sa_dir"], os.path.join(tmp, "accounts"))

    def test_backup_without_rclone_sa_dir_uses_default_service_account_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            store = os.path.join(tmp, "store")
            db_path = os.path.join(tmp, "sa.sqlite3")
            os.mkdir(source)
            os.mkdir(store)
            default_store = service_accounts.DEFAULT_STORE_DIR
            write_json(os.path.join(source, "source.json"), make_service_account("default@example.test"))
            registry = service_accounts.ServiceAccountRegistry(db_path, store)
            registry.import_paths([source])
            messages = []
            old_print_line = common.print_line
            old_refresh = sprinkle._refresh_service_account_quota

            try:
                common.print_line = lambda message="": messages.append(message)
                sprinkle._refresh_service_account_quota = (
                    lambda _account: ({"total": 100, "used": 20, "free": 80}, None)
                )
                service_accounts.DEFAULT_STORE_DIR = store
                sprinkle.read_args([
                    "--drive-id",
                    "drive-id",
                    "--rclone-env-file",
                    os.path.join(tmp, "rclone.env"),
                    "--sa-db",
                    db_path,
                    "--sa-store",
                    store,
                    "backup",
                    "/tmp/local",
                ])
                sprinkle.configure(None)
                sprinkle.prepare_rclone_sa_config()

                self.assertEqual(getattr(sprinkle, "__config")["rclone_sa_dir"], store)
                conf_path = getattr(sprinkle, "__rclone_conf")
                self.assertTrue(os.path.exists(conf_path))
                with open(conf_path) as fp:
                    content = fp.read()
                self.assertIn("service_account_file = ", content)
                self.assertIn("root_folder_id = drive-id", content)
                self.assertTrue(any("--drive-id" in message for message in messages))
                self.assertTrue(any("--rclone-sa-dir" in message for message in messages))
            finally:
                common.print_line = old_print_line
                sprinkle._refresh_service_account_quota = old_refresh
                service_accounts.DEFAULT_STORE_DIR = default_store
                generated = getattr(sprinkle, "__rclone_conf", None)
                if generated and os.path.exists(generated):
                    os.unlink(generated)

    def test_backup_config_uses_only_accounts_with_successful_free_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            store = os.path.join(tmp, "store")
            db_path = os.path.join(tmp, "sa.sqlite3")
            base_conf = os.path.join(tmp, "rclone.conf")
            os.mkdir(source)
            with open(base_conf, "w") as fp:
                fp.write("")
            write_json(os.path.join(source, "ready.json"), make_service_account("ready@example.test", "ready"))
            write_json(os.path.join(source, "full.json"), make_service_account("full@example.test", "full"))
            write_json(os.path.join(source, "failed.json"), make_service_account("failed@example.test", "failed"))
            registry = service_accounts.ServiceAccountRegistry(db_path, store)
            registry.import_paths([source])
            old_refresh = sprinkle._refresh_service_account_quota

            def refresh(account):
                if account["client_email"] == "ready@example.test":
                    return {"total": 100, "used": 25, "free": 75}, None
                if account["client_email"] == "full@example.test":
                    return {"total": 100, "used": 100, "free": 0}, None
                return None, "rclone about failed"

            try:
                sprinkle._refresh_service_account_quota = refresh
                sprinkle.read_args([
                    "--rclone-sa-dir", store,
                    "--drive-id", "drive-id",
                    "--rclone-conf", base_conf,
                    "--sa-db", db_path,
                    "--sa-store", store,
                    "--sa-refresh", "all",
                    "--rclone-env-file", os.path.join(tmp, "rclone.env"),
                    "backup",
                    "/tmp/local",
                ])
                sprinkle.configure(None)
                sprinkle.prepare_rclone_sa_config()
                conf_path = getattr(sprinkle, "__rclone_conf")
                with open(conf_path) as fp:
                    content = fp.read()

                self.assertEqual(content.count("[dst"), 1)
                rows = registry.active_accounts()
                paths = dict((row["client_email"], row["managed_path"]) for row in rows)
                self.assertIn(paths["ready@example.test"], content)
                self.assertNotIn(paths["full@example.test"], content)
                self.assertNotIn(paths["failed@example.test"], content)
            finally:
                sprinkle._refresh_service_account_quota = old_refresh
                generated = getattr(sprinkle, "__rclone_conf", None)
                if generated and os.path.exists(generated):
                    os.unlink(generated)

    def test_backup_skips_known_invalid_service_accounts_without_quota_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            store = os.path.join(tmp, "store")
            db_path = os.path.join(tmp, "sa.sqlite3")
            base_conf = os.path.join(tmp, "rclone.conf")
            os.mkdir(source)
            with open(base_conf, "w") as fp:
                fp.write("")
            write_json(os.path.join(source, "ready.json"), make_service_account("ready@example.test", "ready"))
            write_json(os.path.join(source, "invalid.json"), make_service_account("invalid@example.test", "invalid"))
            registry = service_accounts.ServiceAccountRegistry(db_path, store)
            registry.import_paths([source])
            invalid = next(account for account in registry.active_accounts()
                           if account["client_email"] == "invalid@example.test")
            registry.mark_active_account_invalid(invalid["id"], "Invalid grant: account not found")
            calls = []
            old_refresh = sprinkle._refresh_service_account_quota

            def refresh(account):
                calls.append(account["client_email"])
                return {"total": 100, "used": 20, "free": 80}, None

            try:
                sprinkle._refresh_service_account_quota = refresh
                sprinkle.read_args([
                    "--rclone-sa-dir", source,
                    "--drive-id", "drive-id",
                    "--rclone-conf", base_conf,
                    "--sa-db", db_path,
                    "--sa-store", store,
                    "--sa-refresh", "all",
                    "--rclone-env-file", os.path.join(tmp, "rclone.env"),
                    "backup",
                    "/tmp/local",
                ])
                sprinkle.configure(None)
                sprinkle.prepare_rclone_sa_config()

                self.assertEqual(calls, ["ready@example.test"])
                with open(getattr(sprinkle, "__rclone_conf")) as fp:
                    content = fp.read()
                self.assertEqual(content.count("[dst"), 1)
            finally:
                sprinkle._refresh_service_account_quota = old_refresh
                generated = getattr(sprinkle, "__rclone_conf", None)
                if generated and os.path.exists(generated):
                    os.unlink(generated)

    def test_config_command_writes_home_style_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "sprinkle.conf")
            answers = iter(["", "", "", "", "drive-folder", "", "", "", ""])

            def prompt(_message):
                return next(answers)

            sprinkle.config_command(prompt, output)
            with open(output) as fp:
                content = fp.read()

            self.assertIn("rclone_move=true", content)
            self.assertIn("delete_files=false", content)
            self.assertIn("debug=true", content)
            self.assertIn("rclone_sa_count=20", content)
            self.assertIn("drive_id=drive-folder", content)
            self.assertIn("rclone_sa_dir=/etc/rclone/sa", content)
            self.assertIn("sa_cache_ttl_hours=72", content)
            self.assertIn("sa_refresh=stale", content)
            self.assertIn("sa_clean_invalid=quarantine", content)
            self.assertIn("ls_stop_first=true", content)
            self.assertIn("rclone_env_file=~/.sprinkle/rclone.env", content)
            self.assertIn("rclone_rc_timeout_seconds=30", content)
            self.assertIn("sa_stats_workers=4", content)
            self.assertIn("large_file_threshold_bytes=1073741824", content)

    def test_config_command_defaults_to_home_sprinkle_config_path(self):
        old_home = os.environ.get("HOME")
        old_config = os.environ.get("SPRINKLE_CONFIG")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HOME"] = tmp
            os.environ.pop("SPRINKLE_CONFIG", None)
            answers = iter(["", "", "", "", "", "", "", "", ""])

            def prompt(_message):
                return next(answers)

            try:
                target = sprinkle.config_command(prompt)
                self.assertEqual(target, os.path.join(tmp, ".sprinkle", "sprinkle.conf"))
                self.assertTrue(os.path.exists(target))
                self.assertTrue(os.path.exists(os.path.join(tmp, ".sprinkle", "rclone.env")))
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
                if old_config is None:
                    os.environ.pop("SPRINKLE_CONFIG", None)
                else:
                    os.environ["SPRINKLE_CONFIG"] = old_config

    def test_config_path_precedence_is_cli_then_environment_then_home(self):
        old_home = os.environ.get("HOME")
        old_config = os.environ.get("SPRINKLE_CONFIG")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.environ["HOME"] = tmp
                home_config = os.path.join(tmp, ".sprinkle", "sprinkle.conf")
                env_config = os.path.join(tmp, "environment.conf")
                os.makedirs(os.path.dirname(home_config))
                for path in (home_config, env_config):
                    with open(path, "w") as fp:
                        fp.write("debug=false\n")
                os.environ["SPRINKLE_CONFIG"] = env_config

                sprinkle.read_args(["stats"])
                self.assertEqual(getattr(sprinkle, "__configfile"), env_config)

                sprinkle.read_args(["-c", "~/cli.conf", "stats"])
                self.assertEqual(getattr(sprinkle, "__configfile"), os.path.join(tmp, "cli.conf"))
                self.assertEqual(
                    sprinkle.resolve_config_path("relative.conf", environ={}),
                    "relative.conf",
                )

                os.environ.pop("SPRINKLE_CONFIG", None)
                sprinkle.read_args(["stats"])
                self.assertEqual(getattr(sprinkle, "__configfile"), home_config)
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
                if old_config is None:
                    os.environ.pop("SPRINKLE_CONFIG", None)
                else:
                    os.environ["SPRINKLE_CONFIG"] = old_config

    def test_missing_environment_config_is_an_explicit_error(self):
        old_config = os.environ.get("SPRINKLE_CONFIG")
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing.conf")
            try:
                os.environ["SPRINKLE_CONFIG"] = missing
                sprinkle.read_args(["stats"])
                with self.assertRaisesRegex(Exception, "not found"):
                    sprinkle.configure(getattr(sprinkle, "__configfile"))
            finally:
                if old_config is None:
                    os.environ.pop("SPRINKLE_CONFIG", None)
                else:
                    os.environ["SPRINKLE_CONFIG"] = old_config

    def test_config_command_uses_sprinkle_config_override(self):
        old_config = os.environ.get("SPRINKLE_CONFIG")
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "nested", "sprinkle.conf")
            answers = iter(["", "", "", "", "", "", "", "", ""])

            def prompt(_message):
                return next(answers)

            try:
                os.environ["SPRINKLE_CONFIG"] = target
                written = sprinkle.config_command(prompt)
                self.assertEqual(written, target)
                self.assertTrue(os.path.isfile(target))
            finally:
                if old_config is None:
                    os.environ.pop("SPRINKLE_CONFIG", None)
                else:
                    os.environ["SPRINKLE_CONFIG"] = old_config

    def test_config_file_service_account_defaults_generate_rclone_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            store = os.path.join(tmp, "store")
            db_path = os.path.join(tmp, "sa.sqlite3")
            config_path = os.path.join(tmp, "sprinkle.conf")
            os.mkdir(source)

            write_json(os.path.join(source, "one.json"), make_service_account("one@example.test"))
            write_json(os.path.join(source, "two.json"), make_service_account("two@example.test", "key-two"))
            with open(config_path, "w") as fp:
                fp.write("\n".join([
                    "rclone_move=true",
                    "delete_files=false",
                    "rclone_sa_count=1",
                    "drive_id=drive-id",
                    "rclone_sa_dir=" + source,
                    "rclone_env_file=" + os.path.join(tmp, "rclone.env"),
                    "sa_db=" + db_path,
                    "sa_store=" + store,
                    "rclone_config=" + os.path.join(tmp, "empty-rclone.conf"),
                ]))

            sprinkle.read_args(["-c", config_path, "stats"])
            sprinkle.configure(config_path)
            sprinkle.prepare_rclone_sa_config()
            generated = getattr(sprinkle, "__rclone_conf")
            with open(generated) as fp:
                content = fp.read()

            self.assertTrue(getattr(sprinkle, "__config")["debug"])
            self.assertTrue(getattr(sprinkle, "__config")["ls_stop_first"])
            self.assertFalse(getattr(sprinkle, "__config")["delete_files"])
            self.assertEqual(content.count("[dst"), 1)
            self.assertIn("root_folder_id = drive-id", content)
            os.unlink(generated)


if __name__ == "__main__":
    unittest.main()
