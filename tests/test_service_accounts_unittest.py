import json
import os
import stat
import sys
import tempfile
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
            self.assertTrue(os.path.basename(active[0]["managed_path"]).startswith("sa-"))
            self.assertEqual(stat.S_IMODE(os.stat(active[0]["managed_path"]).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(store).st_mode), 0o700)
            self.assertEqual(len(os.listdir(os.path.join(store, "quarantine"))), 1)

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
            sync.get_best_remote = lambda _size: "dst109:"
            sync.mark_remote_used = lambda _remote, _size: None
            shallow_paths = []
            recursive_paths = []
            copies = []
            sync.ls_shallow = lambda path: shallow_paths.append(path) or {}
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

    def test_sa_stats_refreshes_service_account_file_cache(self):
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
                sprinkle._refresh_service_account_file_cache = (
                    lambda _account: (json.dumps([{
                        "Path": "Movies/Aladin/movie.mkv",
                        "Name": "movie.mkv",
                        "Size": 10,
                        "MimeType": "video/x-matroska",
                        "ModTime": "2024-01-01T00:00:00Z",
                        "IsDir": False,
                        "ID": "file-id",
                    }]), None)
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
            self.assertIsNotNone(cache_row)
            self.assertEqual(cache_row["file_count"], 1)

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
            sync.get_best_remote = lambda _size: "dst109:"
            sync.mark_remote_used = lambda _remote, _size: None
            listed_paths = []
            copies = []
            sync.ls_shallow = lambda path: listed_paths.append(path) or {}
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


class ServiceAccountCliTest(unittest.TestCase):
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

            try:
                common.print_line = lambda message="": messages.append(message)
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
                service_accounts.DEFAULT_STORE_DIR = default_store
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
            self.assertIn("rclone_sa_count=5", content)
            self.assertIn("drive_id=drive-folder", content)
            self.assertIn("rclone_sa_dir=/etc/rclone/sa", content)
            self.assertIn("sa_cache_ttl_hours=72", content)
            self.assertIn("sa_refresh=stale", content)
            self.assertIn("sa_clean_invalid=quarantine", content)
            self.assertIn("ls_stop_first=true", content)
            self.assertIn("rclone_env_file=~/.sprinkle/rclone.env", content)
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
