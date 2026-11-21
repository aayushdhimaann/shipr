# Changelog

## Unreleased

### Security

- shipr no longer writes, reads or deletes through symlinks on the server. A symlinked directory
  or file below `remote_path` or `state_path` used to be written through, overwriting files
  outside the deploy folder even when `protect`ed, or replaced by a regular file. Such paths,
  plus special files and unreadable directories, are now reported as `skip ... left untouched`.
  Every directory is re-checked with `lstat` right before each change.
- An unreadable directory on the server no longer aborts the deploy with a traceback.
- `state_path` overlaps are validated (see breaking changes).
- Leftovers of interrupted uploads are only removed when they are provably shipr's own and
  recoverable. Unrelated `*.shipr-old` and `*.shipr-tmp` files are never deleted. A stale park
  file no longer makes the next upload of that file fail.

- By default, deploy records now live beside the web root instead of inside it:
  `/var/www/html` → `/var/www/.shipr/html`. Records inside the web root warn on every deploy.

### Reliability

- The manifest now names the deployed commit as soon as its files are on the server. Before, a
  failing `remote_after` hook left it naming the previous commit, and `shipr rollback` then
  skipped a version. Hooks that failed or didn't run are re-run by the next deploy, even when no
  file changed (`hooks_pending`).
- A deploy whose manifest or history couldn't be saved now exits non-zero instead of 0. If the
  manifest can't be written before any change, the deploy stops cleanly instead of with a
  traceback.
- Each deploy record notes its stages: `files_done`, `hooks` (none, ok, skipped, failed,
  not-run) and `manifest_saved`. A `remote_before` failure is now recorded in history.
- The manifest records the `project` and `remote_path` it belongs to. A deploy, `status` or
  `rollback` that finds another target's records refuses to use them, so environments that share
  a state dir no longer silently diverge.
- After an interrupted deploy, files whose size or time no longer match the manifest are
  hash-checked first. shipr's own uploads from the interrupted run are no longer mistaken for
  server-side edits and backed up.
- `shipr rollback` is driven by history: deploys whose files reached the server count even if a
  hook failed. After an interrupted deploy it restores the last completed one. It refuses to
  guess when history and manifest disagree.

### Breaking changes

- **New default state location.** Records of new deployments go beside the web root. Existing
  0.1 records at `<remote_path>/.shipr` keep being used, with a warning, until you move them. If
  the deploy user can't create the new default (for example `/var/www` is root-owned), the
  deploy stops before any change and asks you to set `state_path`.
- `state_path` may not equal `remote_path` or be a parent of it. On the same server, environments
  may not share or nest state dirs, or keep state inside each other's `remote_path`. Migration:
  give each environment its own `state_path`, for example `/home/<user>/.shipr/<project>-<env>`.
  When shipr can't tell whether two hosts are one server, it only warns.
- Files at or under a server symlink, special file or unreadable directory are no longer
  deployed. Deploy them by replacing the symlink with a real directory, or by deploying to the
  link's target as its own `remote_path`.
- A leftover parked file whose content shipr can't verify now stops the deploy, with
  instructions. It used to be deleted.
- `RemoteStat` has a new `kind` field and `Provider` a new `roots` attribute. Third-party
  providers keep working unchanged; see docs/providers.md to opt into the symlink checks.

## 0.1.0 (unreleased)

First version.

- Commands: `init`, `validate`, `test`, `deploy`, `status`, `history`, `rollback` and `unlock`.
- SFTP provider:
  - password, key and key-passphrase authentication, plus ssh-agent
  - host key verification, with trust on first use through `test --trust`
  - retries with backoff, and reconnecting after a dropped connection
  - atomic uploads
  - optional shell hooks over SSH
- Local-folder provider. Third-party providers can be added through entry points.
- Incremental deploys that compare file contents against a manifest kept on the server.
- Safety:
  - dry-run, and confirmation for production and for deletions
  - backups of files edited on the server
  - protected paths and built-in secret excludes
  - a lock against concurrent deploys
  - resuming after an interrupted deploy
  - path checks so nothing outside `remote_path` is touched
- Deploy history on the server, and rollback to an earlier commit.
