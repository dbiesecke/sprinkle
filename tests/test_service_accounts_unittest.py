import json
import os
import stat
import sys
import tempfile
import types
import unittest

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
            self.assertIn("large_file_threshold_bytes=1073741824", content)

    def test_config_command_defaults_to_home_sprinkle_config_path(self):
        old_home = os.environ.get("HOME")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HOME"] = tmp
            answers = iter(["", "", "", "", "", "", "", "", ""])

            def prompt(_message):
                return next(answers)

            try:
                target = sprinkle.config_command(prompt)
                self.assertEqual(target, os.path.join(tmp, ".sprinkle", "sprinkle.conf"))
                self.assertTrue(os.path.exists(target))
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home

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
                    "debug=true",
                    "rclone_move=true",
                    "delete_files=false",
                    "rclone_sa_count=1",
                    "drive_id=drive-id",
                    "rclone_sa_dir=" + source,
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
            self.assertFalse(getattr(sprinkle, "__config")["delete_files"])
            self.assertEqual(content.count("[dst"), 1)
            self.assertIn("root_folder_id = drive-id", content)
            os.unlink(generated)


if __name__ == "__main__":
    unittest.main()
