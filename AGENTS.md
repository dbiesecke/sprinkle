# Sprinkle Agent Instructions

This file applies to `sprinkle` and all child paths.

## Service Account Safety

- Google Drive service account JSON files contain secrets. Never print, log, commit, snapshot, or paste raw `private_key` values.
- Use synthetic service-account fixtures in tests. Do not use files from `/Users/user/workspace/svcacc` in committed tests or docs.
- Service-account cleanup must be quarantine-first. Source deletion is allowed only when a user explicitly selects a delete mode.
- Most Google Drive service accounts in this workspace are effectively limited to about 10-15 GB, so code should be designed around many small quotas and cached quota data.
- `sa-import` should validate new accounts with rclone when available. Unknown quota during import is an invalid state and should quarantine the affected account by default.

## Implementation Rules

- Prefer Python stdlib and existing project patterns. Do not add dependencies without explicit approval.
- Keep `--rclone-sa-dir` backward-compatible: existing callers should still get a generated rclone config from service-account files.
- Cache-aware code must keep unknown/error quota states distinct from real byte values. Do not use fake `1` byte quota values as a fallback.
- Avoid filename-based service-account identity. Dedupe by parsed account data such as `client_email`, `private_key_id`, and content hash.
- Large-file upload placement must remain deterministic and capacity-aware. Keep a real free-space headroom check for large files instead of relying on random service-account selection.

## Verification

- Add regression tests for behavior changes, using generated JSON fixtures only.
- Run available local checks before reporting completion. If `pytest` is unavailable, run targeted stdlib checks such as `unittest`, `compileall`, and small synthetic CLI smoke tests.
