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

### Breaking changes

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
