# Changelog

## 1.1.0

- Added rclone environment file support via `rclone_env_file` and `--rclone-env-file`.
- Added first-use rollout of `~/.sprinkle/rclone.env` with defaults for Google Drive chunk size, size-only comparison, and modtime handling.
- Added `--progress` as the preferred progress option and `-v`/`--verbose` to set `RCLONE_VERBOSE=1`; version output remains available through `--version`.
- Fixed rclone JSON parsing when `RCLONE_PROGRESS=1` appends transfer progress to `lsjson` or `about --json` output.
- Improved service-account file listing behavior so missing Drive folders with `--ls-stop-first` do not scan every service-account remote.
- Reduced backup listing work for `delete_files=false` by checking only relevant remote parent directories.
- Added service-account listing cache support and related regression coverage.

## 1.0.0

- Initial packaged Sprinkle release.
