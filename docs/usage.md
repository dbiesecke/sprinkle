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
rclone_sa_count=5
rclone_sa_dir=~/.sprinkle/service-accounts
sa_db=~/.sprinkle/sa-cache.sqlite3
sa_store=~/.sprinkle/service-accounts
sa_cache_ttl_hours=72
sa_refresh=stale
sa_clean_invalid=quarantine
rclone_env_file=~/.sprinkle/rclone.env
```

Service-account JSON contains secrets. Do not print, log, or commit it. Imports with unknown quota or
failed rclone validation are quarantined by default.

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
