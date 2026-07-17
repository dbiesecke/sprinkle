# Google Drive service-account keepalive

Sprinkle can periodically authenticate every managed Google Drive service account without changing
Drive data. The operational runner invokes `sa-stats` with `--sa-refresh=all`, then verifies in the
SQLite registry that every active account received a successful `rclone about --json` result during
that run.

Google does not document automatic deletion of service accounts after five months. Google Cloud can
identify accounts as unused after 90 days and as dormant after more than 180 days. A call to a Google
API with a service account or its key generates authentication activity. A monthly run provides a
large safety margin, but does not protect against manual deletion, disabled keys, deleted projects,
or organization-specific cleanup policies. See Google's
[service-account usage documentation](https://docs.cloud.google.com/policy-intelligence/docs/service-account-usage-tools).

## Install

1. Copy `scripts/sprinkle-sa-keepalive` and `scripts/check-sprinkle-sa-keepalive` to
   `/usr/local/sbin/`, owned by root and executable but not writable by other users.
2. Ensure `/etc/sprinkle/sprinkle.conf` contains an explicit absolute `sa_db` path and points Sprinkle
   at the managed account store. The cron user must be able to read the config, database, account
   files, and rclone executable.
3. Copy `deploy/cron.d/sprinkle-sa-keepalive` to `/etc/cron.d/` and adapt the absolute Sprinkle and
   configuration paths. Keep the external `flock`; parallel execution is intentionally a failure.
4. Connect the daily marker check to the existing monitoring. It exits non-zero when the marker is
   missing, malformed, in the future, or older than 40 days.

Run the job manually before enabling Cron:

```sh
/usr/bin/flock -n /run/lock/sprinkle-sa-keepalive.lock \
  /usr/local/sbin/sprinkle-sa-keepalive \
  --sprinkle /opt/sprinkle/sprinkle.py \
  --config /etc/sprinkle/sprinkle.conf
```

The runner uses a six-hour timeout, writes only sanitized account identity and reason codes to Syslog
under `sprinkle-sa-keepalive`, and returns `1` if Sprinkle fails, no active accounts exist, or any
account did not refresh successfully. A successful run atomically updates the mode-0600 JSON marker
`/var/lib/sprinkle/sa-keepalive.last-success`. Failed runs never update it.

For non-default layouts, `--database`, `--marker`, `--python`, and `--timeout-seconds` provide explicit
overrides. `--no-syslog` is intended for isolated testing.

## Security

The runner discards raw Sprinkle/rclone output. Logs contain only `client_email`,
`project_id`, a fixed reason code, counts, timestamps, and duration. It never reads or logs the
service-account JSON files or private keys. Test only with generated fixtures or an isolated copied
registry; never add real service-account files to the repository.
