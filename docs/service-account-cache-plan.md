# Sprinkle Service-Account Import and Quota Cache

## Goal

Sprinkle now needs to handle very large Google Drive service-account collections without paying the cost of repeated `rclone about`, file listing, and disk-space calls for every account. The design uses a local SQLite registry and cache, backed only by Python stdlib.

## Current Context

- `--rclone-sa-dir` previously scanned one directory of JSON files and generated a temporary rclone config.
- The real workspace account set at `/Users/user/workspace/svcacc` is large and has inconsistent names.
- The account identity is inside the JSON file, so import must not trust filenames.
- Most service accounts are practically small Google Drive quotas, usually around 10-15 GB, so cache freshness matters more than re-querying every account on every operation.

## Implemented Shape

- `sa-import <path...>` recursively imports service-account JSON files into a managed local store and validates new accounts with `rclone about --json`.
- Valid unique accounts are copied into the managed store with stable hash-based filenames.
- Duplicate accounts are recorded but not copied again.
- Invalid accounts, rclone validation errors, and unknown quota results are quarantined by default and can be ignored or deleted only through an explicit cleanup option.
- `sa-stats` reads the registry, refreshes missing or stale quota data depending on the selected mode, and prints cached capacity totals.
- `--rclone-sa-dir` imports and dedupes first, then generates rclone config sections from canonical managed files.
- `sprinkle.py config` writes `~/.sprinkle/sprinkle.conf` interactively and can persist defaults equivalent to `--rclone-sa-count 20 --drive-id XXXXX -d --rclone-sa-dir /etc/rclone/sa`.

## Cache Strategy

- Quota data comes from `rclone about --json`.
- Cached quota fields include total, used, free, trashed, other, object count, refresh time, and last error.
- Missing or unsupported quota fields stay unknown. They are not replaced with fake byte values.
- During `sa-import`, unknown `total` or `free` quota values are treated as validation failures because Sprinkle cannot safely place large files on accounts with unknown capacity.
- Normal backup placement can use cached free space and decrements the cached free/used values after successful writes.
- Large-file placement is still most-free-space based, but it requires extra headroom before selecting a remote: by default files of at least 1 GiB require an additional 512 MiB or 5%, whichever is larger. This keeps large movie uploads away from service accounts that only barely fit the file.
- Cache refresh defaults:
  - `sa-stats`: stale entries refresh by default.
  - normal Sprinkle operations: stale entries refresh by default unless overridden.

## rclone Notes

- rclone VFS cache is useful for mounts and should use an explicit `--cache-dir` and `--vfs-cache-mode writes` or `full`.
- Do not share one VFS cache directory across overlapping remotes.
- rclone `combine` creates a synthetic directory tree from upstreams. It is useful for operator-facing grouped mounts, but it does not replace Sprinkle's most-free-space upload placement.
- If grouping is needed, use small generated groups of about 40-50 service accounts and validate with local fake remotes before using real Google Drive accounts. Sprinkle includes a small config generator for these optional combine groups.

## Verification Notes

- Tests must use synthetic service-account JSON files only.
- The default local paths are:
  - `~/.sprinkle/sa-cache.sqlite3`
  - `~/.sprinkle/service-accounts`
  - `~/.sprinkle/service-accounts/quarantine`
