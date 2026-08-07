# Sprinkle usage

## Configuration location

Sprinkle reads and writes `sprinkle.conf` using one consistent priority:

1. `-c/--conf`
2. a non-empty `SPRINKLE_CONFIG`
3. `~/.sprinkle/sprinkle.conf`

Create the default Home configuration:

```bash
python3 sprinkle.py config
```

Create or use a dedicated configuration:

```bash
SPRINKLE_CONFIG=/etc/sprinkle/sprinkle.conf python3 sprinkle.py config
SPRINKLE_CONFIG=/etc/sprinkle/sprinkle.conf python3 sprinkle.py stats
python3 sprinkle.py -c ./test-sprinkle.conf stats
```

For normal commands a path selected with `-c/--conf` or `SPRINKLE_CONFIG` must exist. Without an
explicit path, Sprinkle loads `~/.sprinkle/sprinkle.conf` when present. Relative explicit paths remain
relative to the current working directory, while `~` is expanded.

The Docker image sets `SPRINKLE_CONFIG=/config/sprinkle.conf`. Mount `/config` to persist it:

```bash
docker run --rm \
  -v "$PWD/config:/config" \
  dbiesecke/sprinkle config
```

## Backup to an explicit rclone remote

Use `remote:path` for an exact destination, or a bare `remote:` for its root:

```bash
python3 sprinkle.py backup /local/movie.mkv hidrive:
python3 sprinkle.py backup /local/roms hidrive:
python3 sprinkle.py backup /local/roms hidrive:/archive
```

The first command copies `movie.mkv` to `hidrive:/movie.mkv`. The second copies the directory beneath
`hidrive:/roms/`; the third copies its contents beneath `hidrive:/archive/`. Single-file backups never
delete unrelated objects from the target directory.

## Rotating service-account union backups

`backup-union` is an opt-in clustered backup mode that writes each batch through an rclone Union remote:

```bash
python3 sprinkle.py --drive-id DRIVE_ID --rclone-sa-count 20 backup-union /local/roms
```

Only active service accounts with a known usable quota are selected. On a confirmed Google
`storageQuotaExceeded` response, Sprinkle closes that batch and continues with one fresh random batch.
Completed batches remain read-only union upstreams, so a later invocation sees files already stored by
earlier batches. Batch metadata (account IDs, order, status, and timestamps) is kept in `sa_db`; credentials
are never stored there. `--rclone-move`, `--dry-run`, `--delete-files`, explicit targets, Drive roots, and
the large-file free-space headroom rule apply as they do for `backup`.

## Rclone configuration isolation

Sprinkle ignores `RCLONE_CONFIG` from both the process environment and `rclone_env_file`. This avoids
an old production value selecting the wrong account during commands such as `rclone about`.

Choose a classic rclone configuration explicitly:

```bash
python3 sprinkle.py --rclone-conf "$HOME/.config/rclone/rclone.conf" stats
```

The equivalent `sprinkle.conf` setting is:

```ini
rclone_config=/home/user/.config/rclone/rclone.conf
```

When neither setting is present, classic remotes use rclone's normal default location. Service-account
operations do not use that fallback: Sprinkle generates a temporary configuration for the selected
account, supplies it through `--config`, and removes it after the operation.

## Logging

Debug logging is enabled by default. Sprinkle also applies the configured level to existing root handlers,
so earlier library logging cannot silently suppress its diagnostics. Set `debug=false` in `sprinkle.conf`
when only normal informational output is wanted.

## Service-account example

Import synthetic or real account files into the managed store, then refresh every active account:

```bash
python3 sprinkle.py sa-import /secure/incoming-service-accounts
python3 sprinkle.py --drive-id GDRIVE_FOLDER_ID --sa-refresh=all sa-stats
```

Recommended `sprinkle.conf` values:

```ini
drive_id=GDRIVE_FOLDER_ID
rclone_sa_count=20
rclone_sa_dir=~/.sprinkle/service-accounts
sa_db=~/.sprinkle/sa-cache.sqlite3
sa_store=~/.sprinkle/service-accounts
sa_cache_ttl_hours=72
sa_refresh=stale
sa_clean_invalid=quarantine
sa_delete_account_not_found=false
rclone_env_file=~/.sprinkle/rclone.env
```

## Optional rclone RC transport

Set an RC URL to run listings, quota queries, transfers, and deletions through the RC server instead of
starting local rclone processes:

```ini
rclone_rc_url=http://rclone.example.invalid
rclone_rc_user=quota-reader
rclone_rc_password=store-this-outside-version-control
rclone_rc_timeout_seconds=30
sa_stats_workers=4
# Use reusable RC Drive slots for backup placement.
rclone_rc_remotes=dst101,dst102
# Map literal backup paths to this local remote on the RC host.
rclone_rc_local_remote=mylocal
```

The corresponding command-line options are `--rclone-rc-url`, `--rclone-rc-user`,
`--rclone-rc-password`, `--rclone-rc-timeout-seconds`, and `--sa-stats-workers`. RC never falls back to
local rclone after an error. `rclone_rc_remotes` names a fixed pool of reusable RC Drive slots; Sprinkle installs
the selected credential immediately before a slot is used and rotates it only after the current account cannot
fit the next upload. `rclone_rc_local_remote` maps a literal path such as `/srv/media` to
`mylocal:/srv/media` on the RC host, so the source files must be present there. HTTP and HTTPS RC endpoints are
both supported; authenticate the server when it is reachable by other hosts and do not commit credentials.
Set `rclone_rc_remotes` to use reusable RC Drive slots and skip local service-account rclone-config generation.
Prefer the `SPRINKLE_RCLONE_RC_PASSWORD` environment variable over
storing an RC password in a config file.
For clustered Google Drive backups, `drive_id` is required even in RC mode. Sprinkle passes it as rclone's
per-remote `root_folder_id` override for every configured `dst*` destination, while leaving `mylocal:` and
explicit non-cluster remotes unchanged.

Service-account JSON contains secrets. Do not print, log, or commit it. Duplicate account JSON is counted and
skipped without adding a managed file or SQLite account record. `sa-import` always validates candidates locally;
with RC configured, it validates in parallel using `sa_stats_workers`. Imports with unknown quota or failed
validation are quarantined by default. RC configuration errors are reported separately and do not quarantine a
credential that validated locally.

Use `--sa-delete-account-not-found` only when source JSON files should be removed after the exact Google
error `Invalid grant: account not found`. Other quota and credential errors never trigger this deletion.
Use `--sa-delete-rc-http-500` only for an intentional destructive cleanup: it refreshes all active accounts
and removes the managed and source JSON files for accounts whose RC quota request returns HTTP 500.
Known invalid accounts are skipped during later backups, avoiding repeated `rclone about` calls.

`backup` refreshes missing or stale quota data before generating its clustered rclone configuration.
Only active accounts with a successful quota check and positive known free space are included; file-size
placement still applies its normal per-upload capacity check. `sa-stats` refreshes account quotas only and
does not recursively list every Drive file, so it remains suitable for very large Drive folders.

## Backup failures

Backup continues after an individual quota, transfer, update, or deletion failure. For new files,
Sprinkle tries every remote with known sufficient free space, starting with the most free capacity.
Unknown quota is never used as capacity and is checked again for later files. At the end, unresolved
operations are reported together and the command exits non-zero, preserving existing SMTP and cron
failure handling. Fix the reported remote or quota error and rerun the backup.
When rclone returns Google `storageQuotaExceeded`, Sprinkle records `free=0` for that remote in memory
and the service-account quota cache, excludes it for the remainder of that backup run, then tries the next eligible
remote for a new file.
Other RC transfer failures do not falsify the quota cache: Sprinkle excludes the failed remote only for
the current backup run, preventing repeated attempts for every later file. A failed quota query is treated
the same way but retains an unknown quota rather than marking the remote full. A later run checks it again.

After a successful clustered add or update, Sprinkle confirms the visible target file before applying its
actual size delta to SQLite. Successful file deletion releases the known former file size. A failed target
confirmation leaves quota values unchanged and makes the backup fail clearly rather than storing a guessed
capacity value.

## Explicit classic rclone targets

Backups to an explicit classic rclone target support backends without object IDs,
including rclone's `local` backend:

```bash
python3 sprinkle.py backup /data/Manga local:/srv/backups/Manga
```

With `delete_files=true`, files and directories removed from the source are also
removed from the target. With `delete_files=false`, additional target objects are
left untouched.
