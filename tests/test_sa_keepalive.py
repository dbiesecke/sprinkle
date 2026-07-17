import datetime
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "scripts", "sprinkle-sa-keepalive")
CHECKER = os.path.join(ROOT, "scripts", "check-sprinkle-sa-keepalive")


FAKE_SPRINKLE = r'''
import datetime
import os
import sqlite3
import sys

database = os.environ["FAKE_SA_DB"]
failed_email = os.environ.get("FAKE_FAILED_EMAIL")
now = datetime.datetime.utcnow().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
with sqlite3.connect(database) as connection:
    rows = connection.execute("SELECT id, client_email FROM accounts WHERE status='active' ORDER BY id").fetchall()
    for account_id, email in rows:
        if email == failed_email:
            connection.execute(
                "UPDATE quota_cache SET last_error=?, updated_at=? WHERE account_id=?",
                ("invalid_grant -----BEGIN PRIVATE KEY----- SECRET", now, account_id),
            )
        else:
            connection.execute(
                "UPDATE quota_cache SET last_about_at=?, last_error=NULL, updated_at=? WHERE account_id=?",
                (now, now, account_id),
            )
sys.exit(int(os.environ.get("FAKE_EXIT_STATUS", "0")))
'''


def create_database(path, accounts):
    old = "2000-01-01T00:00:00Z"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY,
                client_email TEXT,
                project_id TEXT,
                status TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE quota_cache (
                account_id INTEGER PRIMARY KEY,
                last_about_at TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        for account_id, email, status in accounts:
            connection.execute(
                "INSERT INTO accounts (id, client_email, project_id, status) VALUES (?, ?, ?, ?)",
                (account_id, email, "project-{}".format(account_id), status),
            )
            connection.execute(
                "INSERT INTO quota_cache (account_id, last_about_at, last_error, updated_at) VALUES (?, ?, NULL, ?)",
                (account_id, old, old),
            )


class KeepaliveRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.tempdir.name, "sa.sqlite3")
        self.config = os.path.join(self.tempdir.name, "sprinkle.conf")
        self.fake = os.path.join(self.tempdir.name, "fake_sprinkle.py")
        self.marker = os.path.join(self.tempdir.name, "state", "last-success")
        with open(self.config, "w") as handle:
            handle.write("sa_db={}\n".format(self.database))
        with open(self.fake, "w") as handle:
            handle.write(FAKE_SPRINKLE)

    def tearDown(self):
        self.tempdir.cleanup()

    def invoke(self, extra_env=None):
        environment = os.environ.copy()
        environment["FAKE_SA_DB"] = self.database
        environment.update(extra_env or {})
        return subprocess.run(
            [
                sys.executable,
                RUNNER,
                "--sprinkle",
                self.fake,
                "--config",
                self.config,
                "--database",
                self.database,
                "--marker",
                self.marker,
                "--no-syslog",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )

    def test_success_refreshes_every_account_and_writes_private_marker(self):
        create_database(self.database, [
            (1, "one@example.test", "active"),
            (2, "two@example.test", "active"),
            (3, "ignored@example.test", "invalid"),
        ])

        result = self.invoke()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=success total=2 succeeded=2 failed=0", result.stderr)
        with open(self.marker) as handle:
            marker = json.load(handle)
        self.assertEqual(marker["total"], 2)
        self.assertEqual(marker["succeeded"], 2)
        self.assertEqual(stat.S_IMODE(os.stat(self.marker).st_mode), 0o600)
        with sqlite3.connect(self.database) as connection:
            rows = connection.execute(
                "SELECT last_about_at, last_error FROM quota_cache WHERE account_id IN (1, 2)"
            ).fetchall()
        self.assertTrue(all(row[0] != "2000-01-01T00:00:00Z" and row[1] is None for row in rows))

    def test_partial_failure_checks_all_accounts_and_preserves_marker(self):
        create_database(self.database, [
            (1, "one@example.test", "active"),
            (2, "two@example.test", "active"),
        ])
        os.makedirs(os.path.dirname(self.marker))
        with open(self.marker, "w") as handle:
            handle.write("unchanged")

        result = self.invoke({"FAKE_FAILED_EMAIL": "one@example.test"})

        self.assertEqual(result.returncode, 1)
        self.assertIn("client_email=one@example.test", result.stderr)
        self.assertIn("reason=credentials_rejected", result.stderr)
        self.assertIn("status=failed total=2 succeeded=1 failed=1", result.stderr)
        self.assertNotIn("PRIVATE KEY", result.stderr)
        self.assertNotIn("SECRET", result.stderr)
        with open(self.marker) as handle:
            self.assertEqual(handle.read(), "unchanged")
        with sqlite3.connect(self.database) as connection:
            refreshed = connection.execute(
                "SELECT last_about_at FROM quota_cache WHERE account_id=2"
            ).fetchone()[0]
        self.assertNotEqual(refreshed, "2000-01-01T00:00:00Z")

    def test_no_active_accounts_fails_without_marker(self):
        create_database(self.database, [(1, "invalid@example.test", "invalid")])

        result = self.invoke()

        self.assertEqual(result.returncode, 1)
        self.assertIn("status=failed total=0", result.stderr)
        self.assertFalse(os.path.exists(self.marker))

    def test_nonzero_sprinkle_status_fails_even_after_refresh(self):
        create_database(self.database, [(1, "one@example.test", "active")])

        result = self.invoke({"FAKE_EXIT_STATUS": "7"})

        self.assertEqual(result.returncode, 1)
        self.assertIn("command_status=7", result.stderr)
        self.assertFalse(os.path.exists(self.marker))

    def test_missing_database_is_sanitized_operational_failure(self):
        result = self.invoke()

        self.assertEqual(result.returncode, 1)
        self.assertIn("reason=operational_error", result.stderr)
        self.assertNotIn(self.database, result.stderr)


class KeepaliveMarkerCheckTest(unittest.TestCase):
    def invoke(self, marker, max_age="40"):
        return subprocess.run(
            [sys.executable, CHECKER, "--marker", marker, "--max-age-days", max_age],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_recent_marker_is_ok(self):
        with tempfile.TemporaryDirectory() as tempdir:
            marker = os.path.join(tempdir, "marker")
            completed = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(marker, "w") as handle:
                json.dump({"completed_at": completed, "total": 4}, handle)

            result = self.invoke(marker)

            self.assertEqual(result.returncode, 0)
            self.assertIn("OK", result.stdout)
            self.assertIn("accounts=4", result.stdout)

    def test_old_or_missing_marker_is_critical(self):
        with tempfile.TemporaryDirectory() as tempdir:
            marker = os.path.join(tempdir, "marker")
            old = (
                datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=41)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(marker, "w") as handle:
                json.dump({"completed_at": old, "total": 4}, handle)

            old_result = self.invoke(marker)
            missing_result = self.invoke(os.path.join(tempdir, "missing"))

            self.assertEqual(old_result.returncode, 1)
            self.assertIn("CRITICAL", old_result.stdout)
            self.assertEqual(missing_result.returncode, 1)
            self.assertIn("CRITICAL", missing_result.stdout)


if __name__ == "__main__":
    unittest.main()
