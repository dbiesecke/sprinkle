# Sprinkle (Volume Clustering)


Sprinkle is a volume clustering utility. It presents all the RClone available volumes as a single clustered volume. It supports 1-way sync mainly for
backup and recovery. Sprinkle uses the excellent [RClone](https://rclone.org) software for cloud volume access.

## Special Features

* Docker Image [dbiesecke/sprinkle](https://hub.docker.com/r/dbiesecke/sprinkle)
  
* load sa accounts from directory with import, dedupe, gdrive_id option & limiter

```bash
# will load 20 SA accounts from /etc/rclone/sa; root_folder is set to "XXXXX" (your public Google Drive directory)

$ docker run -i -v /etc/rclone:/etc/rclone:ro dbiesecke/sprinkle --rclone-sa-count 20 --drive-id XXXXX -d --rclone-sa-dir /etc/rclone/sa stats
```

* import, dedupe, validate, quarantine, and cache Google Drive service account quota data

```bash
$ ./sprinkle.py sa-import /etc/rclone/sa
$ ./sprinkle.py --drive-id XXXXX sa-stats
$ ./sprinkle/sprinkle.py -d --drive-id YouDriveID backup /Users/user/workspace/Movies/Aladin
$ ./sprinkle.py backup /local/movie.mkv hidrive:
$ ./sprinkle.py backup /local/roms hidrive:

# rotate service-account batches through an rclone Union read view
$ ./sprinkle.py --drive-id XXXXX --rclone-sa-count 20 backup-union /local/roms

```

`sa-import` validates new accounts locally with `rclone about --json` and prints per-file progress. Validation uses the existing `sa_stats_workers` setting (default: 4), while registry writes and progress remain ordered. Duplicate account JSON is counted and skipped without creating an additional managed file or SQLite account record. If validation fails or quota remains unknown, the account is quarantined by default.

An explicit rclone target accepts both `remote:path` and a bare `remote:`. A local file sent to `hidrive:`
lands in the remote root. A local directory sent to `hidrive:` lands under a same-named folder, while
`hidrive:/archive` uses `/archive` exactly. Single-file backups never delete other objects in the target.

For already imported accounts, `--sa-delete-account-not-found` is an explicit cleanup option. It removes
the managed and source JSON only when Google returns `Invalid grant: account not found`; other validation
and quota failures remain non-destructive.
`--sa-delete-rc-http-500` is a separate destructive option: it refreshes every active account and removes
only accounts whose RC quota request explicitly returns HTTP 500. Use it only when that response is known
to identify a permanently bad service account.
Known invalid accounts are skipped during later backups, so they do not trigger repeated quota checks.

* run an auditable monthly Google Drive service-account keepalive through Cron

For unattended monthly authentication checks, install the operational runner described in
[docs/sa-keepalive.md](docs/sa-keepalive.md). It forces a read-only refresh for every active account,
verifies the SQLite results, maintains a success marker for monitoring, and never logs key material.

Sprinkle keeps upload placement capacity-aware for large files: service accounts are selected by known free space, with extra headroom for files of at least 1 GiB so a large movie is not sent to an account that only barely fits it.

Backups continue after individual transfer, quota, update, or deletion failures. New files try each
capacity-qualified remote in order of available space. Any unresolved operations are summarized at the
end and return a non-zero status, so scheduled jobs and SMTP alerts remain reliable. Resolve the remote
or quota problem and rerun the backup; already completed files are not uploaded again.
Before clustered backups, Sprinkle only generates remotes for active service accounts with a successful,
positive free-space quota. `sa-stats` refreshes quota data without recursively listing the Drive contents.
If rclone reports Google `storageQuotaExceeded` during a copy, Sprinkle marks that remote as full, stores
`free=0` in the quota cache, and excludes it for the remainder of that backup run before trying the next eligible
remote. A different RC transfer error
only excludes that remote for the current backup run, so repeated files do not retry it indefinitely; a
new Sprinkle run considers it again after its normal quota check. A failed quota query also excludes that
remote only for the current run, while retaining its quota as unknown rather than marking it full.

After a successful clustered add or update, Sprinkle confirms the target file and applies its observed
size delta to cached `used` and `free` values. Successful file deletions release their known old size.
An unconfirmed target leaves the cache unchanged and is reported as an accounting failure.

To run all rclone operations on an existing rclone RC server, configure `rclone_rc_url`, optional
`rclone_rc_user`/`rclone_rc_password`, `rclone_rc_timeout_seconds=30`, and `sa_stats_workers=4`.
Sprinkle uses the RC operations and sync endpoints and never falls back to local rclone after an RC failure.
For an ordinary
absolute backup path, set `rclone_rc_local_remote=mylocal` (or the server's local remote name): Sprinkle then
reads `/path` as `mylocal:/path` on the RC host. Keep RC private and authenticated, and never
commit its credentials. Prefer `SPRINKLE_RCLONE_RC_PASSWORD` over storing the password in a config file.

Set `rclone_rc_remotes=dst101,dst102` to define a fixed pool of reusable Drive slots. Before a slot is used,
Sprinkle sends the selected service-account credential to the RC server with `config/update`; it rotates a slot
only when its account cannot fit the next upload with the configured headroom. This mode still requires
`drive_id`: Sprinkle applies it as rclone's `root_folder_id` override to each clustered destination. The RC
endpoint may use either `http://` or `https://`; authentication is recommended whenever it is reachable by
other hosts.

* create a home-directory configuration with interactive defaults

```bash
$ ./sprinkle.py config
# writes ~/.sprinkle/sprinkle.conf, including:
# --rclone-sa-count 20 --drive-id XXXXX -d --rclone-sa-dir /etc/rclone/sa
# rclone_env_file=~/.sprinkle/rclone.env
# sa_cache_ttl_hours=72
# sa_refresh=stale
# sa_clean_invalid=quarantine
```

Sprinkle resolves its configuration in this order: `-c/--conf`, then a non-empty
`SPRINKLE_CONFIG`, then `~/.sprinkle/sprinkle.conf`. The `config` command uses the
same order when choosing where to write. An explicit CLI or environment path must
exist for normal commands; the Home file is optional.

`~/.sprinkle/rclone.env` is created on first use and exports rclone tuning defaults
such as `RCLONE_DRIVE_CHUNK_SIZE=256M`, `RCLONE_SIZE_ONLY=1`, and
`RCLONE_NO_UPDATE_MODTIME=1`. Lines beginning with `#` are ignored.
`RCLONE_CONFIG` is reserved and ignored, including when inherited from production
or written into `rclone.env`. Use `--rclone-conf`, `rclone_config`, or Sprinkle's
generated service-account configuration instead.

The Docker image sets `SPRINKLE_CONFIG=/config/sprinkle.conf`, so a mounted
`/config` directory intentionally overrides the Home default:

```bash
docker run --rm \
  -v "$PWD/config:/config" \
  dbiesecke/sprinkle config
```

See [docs/usage.md](docs/usage.md) for configuration and service-account examples.



Features:
* Consolidate multiple cloud drives into a single virtual drive
* Sprinkle your backup across multiple cloud drives
* Minimize cost by stacking multiple free cloud drives into single one
* Run as Unix daemon with custom schedules for seamless backups of important files
* Developed in Python for extreme multi-platform flexibility

## Getting Started

The easiest way to install Sprinkle and all prerequisites is via PyPI with:
```
pip3 install git https://gitlab.com/dbiesecke/sprinkle.git
```

Or by cloning the repository to your running machine, but make sure prerequisites are met:
```
git clone https://gitlab.com/dbiesecke/sprinkle.git
cd sprinkle
sprinkle.py config
sprinkle.py sa-import /your/sa/accounts
sprinkle.py sa-stats
sprinkle.py -d --drive-id YourDrive backup Movies/Aladin  
```
A more comprehensive guide can be found [here](https://dbiesecke.github.io/sprinkle/docs/guide)

## Prerequisites

* Python 3 installed
* FileLock Python library [https://pypi.org/project/filelock](https://pypi.org/project/filelock)
* Progress Python library [https://pypi.org/project/progress](https://pypi.org/project/progress)
* RClone installed and available in the PATH or configured in sprinkle.conf file. RClone documentation
is available [here](https://rclone.org) for reference
* Few storage drives available from the supported RClone drives

## Installing

Following are the installation steps:

* Install Sprinkle with a supported method
* Download and install RCLone from [https://rclone.org](https://rclone.org)
* Run **RClone** config to configure and authorize your cloud or local storage
  (you might want to run the program on a machione for which http://localhost can be reached
  ideally, from your local workstation)
* Verify access to the storage by issuing the command "rclone ls {alias name}:"
* Copy rclone.conf on the machine which will execute Sprinkle
* Make sure all the prerequisites are satisfied
* Add **RClone** executable to the system PATH variable, or configure location in sprinkle.conf file
* From Sprinkle installation directory run **"./sprinkle.py [-c path to sprinkle.conf] ls /"**

From this point, backups and restore can be executed on the clustered storage.

```
./sprinkle.py -c {path to sprinkle.conf} backup {directory to backup}
```

You can also bypass clustered placement and back up to one explicit rclone target
from the normal rclone config:

```
./sprinkle.py backup /dir_to_backup hidrive:public/Manga
```

Both source and target can be rclone remotes, so no local staging directory is
required:

```
./sprinkle.py backup hidrive:public/Manga backup:mirror/Manga
```

Explicit targets also support classic rclone backends that do not expose object
IDs, including absolute local targets such as `local:/srv/backups/Manga`.

When Sprinkle generates a temporary service-account rclone config, it also includes
the existing rclone config, so configured remotes such as `hidrive:` remain
available. Clustered placement is still limited to the generated service-account
remotes unless an explicit target is supplied.

Use the builtin --help utility to get additional commands and information.

```
./sprinkle.py --help
```

and the command specific help.

```
    -c, --conf {config file}     configuration file
    -d, --debug                  debug output (default:true)
    -h, --help                   help
    -v, --verbose                set RCLONE_VERBOSE=1 for rclone
    --version                    print version
    --check-prereq               chech prerequisites
    --comp-method {size|md5}     compare method [size|md5] (default:size)
    --daemon-interval            interval for the daemon to execute in minutes (default:60)
    --daemon-mode                start sprinkle in daemon mode
    --daemon-pidfile             daemon pidfile (default:/var/run/sprinkle.pid or /tmp/sprinkle.pid)
    --daemon-type                type of daemon [interval|ondemand] (default:interval)
    --delete-files               do not delete files on remote end (default:false)
    --display-unit {G|M|K|B}     display unit (G)igabytes, (M)egabytes, (K)ilobytes, or (B)ites
    --dist-type {mas}            distribution type (default:mas)
    --dry-run                    perform a dry run without actually backing up
    --exclude-file {file}        file containing the backup exclude paths
    --exclude-regex {regex}      regular expression to match for file backup exclusion
    --log-file {file}            logs output to the specified file
    --no-cache                   turn off caching
    --rclone-conf {config file}  rclone configuration (default:None)
    --rclone-env-file {file}     file with environment variables for rclone
    --rclone-sa-dir {dir}        build rclone config from service accounts
    --rclone-sa-count {num}      limit number of service accounts used
    --drive-id {id}              Google Drive folder ID for rclone config
    --sa-db {file}               service account registry database
    --sa-store {dir}             managed service account store
    --sa-cache-ttl-hours {num}   hours before cached SA quota is stale (default:72)
    --sa-refresh {mode}          SA quota refresh [missing|stale|all|none] (default:stale)
    --sa-clean-invalid {mode}    invalid SA cleanup [none|quarantine|delete] (default:quarantine)
    --sa-group-size {num}        preferred SA grouping size for generated operator configs
    --rclone-exe {rclone_exe}    rclone executable (default:rclone)
    --rclone-move                use 'rclone move' instead of 'rclone copy' (default:false)
    --restore-duplicates         restore files if duplicates are found (default:false)
    --retries {num_retries}      number of retries (default:1)
    --progress                   show progress
    --single-instance            make sure only 1 concurrent instance of sprinkle is running (default:False)
    --ls-stop-first              stop listing after first remote with files (default:true)
    
```

## Authors

* **Michael Montuori** - *Head developer* - [mmontuori](https://gitlab.com/mmontuori)
* **Daniel** - *Fork* [dbiesecke](https://gitlab.com/dbiesecke)
#
## License

This project is licensed under the GPLv3 License - see the
[LICENSE](https://www.gnu.org/licenses/gpl-3.0.en.html) file for details
