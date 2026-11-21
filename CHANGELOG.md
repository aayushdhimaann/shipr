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

- By default, deploy records now go beside `remote_path` instead of inside it: `/var/www/html`
  → `/var/www/.shipr/html`. This is outside the site only when `remote_path` is the web root
  itself, so the first deploy to the default location warns about it. Records inside
  `remote_path` warn on every deploy. Records in both the old and new locations (a half-finished
  move) stop the deploy instead of being guessed between.

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

### SSH and secrets

- New `sftp.host_key` setting pins the server's key fingerprint (`SHA256:...`, or a list while
  rotating keys). It wins over known_hosts and needs no known_hosts entry, which is what CI
  needs.
- `--trust` now shows the fingerprint and asks for confirmation in a terminal before saving a
  key. Without a terminal it refuses, and `--yes` never confirms a host key. The fingerprint is
  shown with a single `SHA256:` prefix (it was doubled), and "not a known host" errors print it.
- `known_hosts` lines are appended in one write and not duplicated.
- Passwords and passphrases are held as secret values that `repr()` and tracebacks don't show.
- New `build.env` allowlist: local build commands and hooks see only the named variables plus a
  few basics. Unset, they still see everything, as before.
- `sha256sum` output from the server is only trusted when it matches the request exactly.
  Otherwise shipr downloads and hashes the files itself, so a crafted file name can no longer
  forge another file's hash (C-16).

### Network failures (Phase 3A)

- A rename or delete that the server carried out, but whose reply was lost when the connection
  dropped, is no longer retried blindly and reported as failed. After reconnecting, shipr checks
  the server:
  - A delete counts as done if the file is gone.
  - A delete-to-backup rename counts as done only if the backup has the size and `sha256sum`
    hash the file had before the move.
  - When shipr can't prove the outcome (for example the server can't run `sha256sum`), it
    reports the new `AmbiguousOperation` error and leaves both paths as they are.
- Lock release no longer warns spuriously after a lost reply.
- A connection that never comes back still stops the deploy with `ConnectionLost`, keeps the
  lock and the interrupted-deploy marker, and repeats nothing. The next deploy, after
  `shipr unlock`, finishes the job.

### Hook execution (Phase 3B)

- Remote hooks could hang forever and hold the deploy lock. Each hook command now has a timeout,
  `hooks.timeout` (default 1800 seconds; `null` disables it). A timed-out hook fails the deploy,
  is not retried in that run, and runs again on the next deploy. The message says the remote
  command may still be running, since SSH can't promise to stop it.
- A remote hook writing more than about 2 MB to stderr deadlocked the deploy (reproduced on
  OpenSSH). stderr is now merged into stdout and read as it arrives.
- Remote hook output is shown while the command runs, not after it ends.
- A connection that fails while a hook runs is a clean error, not a traceback.
- Local hooks and remote hooks on the `local` provider run without stdin, in their own process
  group, so a timeout or Ctrl-C also stops the processes they started.
- `sha256sum` batches time out after 300 seconds and fall back to downloading.
- `Provider.exec` accepts an optional `on_output` callback. Providers without it keep working.

### Hook scheduling and shell detection (Phase 3C)

- A deploy whose only pending change was files kept by `--no-delete` (or a declined deletion
  prompt) re-ran every remote hook and wrote a new history record each time. It is now "up to
  date": no hooks run and no records are written. The kept files are still listed, and a later
  deploy with deletion confirmed removes them.
- With `ssh_exec: auto`, a shell check that timed out or lost its connection was taken to mean
  "SFTP-only server", so remote hooks such as migrations were silently skipped and the deploy
  reported success. Now only an explicit refusal counts as "no shell": OpenSSH's "This service
  allows sftp connections only.", a nologin shell, or an administratively refused session.
  Anything else stops the deploy before any change, when remote hooks are configured.
- The shell check runs before any prompt or change, not after the confirmation.
- `ssh_exec: false` now says it skipped hooks deliberately, instead of "no shell access".
- New optional `Provider.exec_probe_error()` for providers that probe shell support.

### Configuration and secret hygiene (Phase 3D)

- A key given twice in one mapping of `shipr.yml` or the global config is now an error naming
  both lines. YAML used to keep the last value silently, so a second `production:` could
  redirect a deploy. Overriding a value brought in by a `<<: *anchor` merge is still allowed.
- Invalid YAML errors no longer quote the offending line, which could hold a password. They give
  the line and column instead.
- Provider settings are checked by `shipr validate` (and every command), not only when
  connecting: misspelled keys, missing `host` or `username`, malformed `host_key` pins, and
  out-of-range `port`, `timeout` or `retries`. Errors name the setting, never its value.
- New optional `Provider.validate_settings(settings)` classmethod for third-party providers.
  Providers without it keep working.
- Build commands and local hooks without a `build.env` allowlist now get a warning, in
  `shipr validate` and on each deploy, naming the variables that supply provider settings (for
  example `SHIPR_PASSWORD (sftp.password)`), never their values. Nothing is hidden from the
  commands yet. **Planned for the next major release:** variables that supply provider settings
  will be hidden from local commands by default, unless `build.env` names them.
- `files.default_excludes: false` now warns in `shipr validate` (listing the matching committed
  files) and on each deploy (listing the files in that deploy, such as `.env`, that the built-in
  excludes would have kept back). File names only; what gets uploaded is unchanged.

### Lock and rollback (Phase 3E)

- A deploy only ever removes its own lock. Each lock holds a random token and is compared byte for
  byte, instead of searching for the deploy id in the lock's text. The SFTP provider re-reads the
  lock before every delete attempt, so a retry after a lost reply no longer deletes a lock another
  deploy took meanwhile. A lock removed or taken over while the deploy waited at a prompt stops it
  before any change; one taken over while it ran is left in place, with a warning to check
  `shipr status` and `shipr history`.
- The "another deploy holds the lock" error and `shipr status` show the holder's deploy id, user,
  machine and age, and the error prints the exact `shipr unlock -e ENV --id ...` command. A lock
  that can't be released names that command too.
- `shipr unlock` shows the holder before asking, and removes only the lock it showed. New
  `--id DEPLOY_ID` removes the lock only if that deploy holds it, without a prompt.
- `rollback --to` no longer picks one of several commits silently. An exact deploy id wins; a
  commit prefix must have at least 4 hex digits and match exactly one deployed commit, else the
  candidates are listed.
- The history and manifest `branch` is recorded only when the deployed ref is a branch. A commit,
  a tag, or a rollback used to be recorded as the "branch".
- New optional `Provider.delete_if_content(path, expected)`; the default works for any provider.

### Filesystems, Python versions and temp files (Phase 3F)

- With `state_path` on another filesystem than `remote_path`, every deletion used to fail
  ("Failure" from OpenSSH, EXDEV locally) and the deploy ended partial. The file is now copied
  into the backup folder, the copy is verified by SHA-256 against the original, and only then is
  the original removed. An existing backup is never overwritten, a copy that doesn't match keeps
  the original, and an interruption at any step leaves the original in place (P-1).
- Python 3.10.0–3.10.11 and 3.11.0–3.11.3 crashed on every deploy with a `TypeError` traceback
  (their `tarfile` has no extraction filter). They are now excluded by `requires-python`, and
  shipr itself stops with a clear error on them before doing anything. The git export is still
  only extracted with the `data` filter: there is no unfiltered fallback (P-2).
- An upload's temporary file on the server is created 0600 in the open request itself, and gets
  its final mode only once complete. It used to be created with the server's default mode
  (often 0644), so other users on a shared host could read a 0600 file's new content while it
  uploaded. Final modes are unchanged. The local provider does the same (P-5).

### Breaking changes

- **Python 3.10.12+, 3.11.4+ or 3.12+ is required.** Earlier patch releases already failed on
  every deploy; now they are refused up front. (pip refuses to install on them; uv's `pip
  install` only checks the lower bound, so on 3.11.0–3.11.3 shipr installs and then stops with
  the error above.)

- **`shipr unlock --yes` no longer removes a lock on its own.** It shows the holder and exits 3.
  Use `shipr unlock --id <deploy id>` in scripts.
- **`rollback --to` needs at least 4 characters of a commit**, or a deploy id, and refuses a
  prefix that matches several deployed commits.
- **`RemoteState.release_lock()` takes no argument**: a `RemoteState` releases only the lock it
  created.

- **`shipr validate` and every command now reject invalid provider settings and duplicate YAML
  keys** that used to load. Such configs failed on connect (provider settings) or silently used
  the last value (duplicate keys). Fix the setting the error names.

- **An unclear shell check with remote hooks configured now stops the deploy** instead of
  skipping the hooks. Set `ssh_exec: false` if the server has no shell and the hooks should be
  skipped.
- **Hooks now have a 30-minute default timeout.** Set `hooks.timeout: null` for hooks that
  legitimately run longer.
- **`local_before`, `local_after` and `local`-provider hooks no longer read from your
  terminal**: they get no stdin, so an interactive prompt fails instead of waiting.
- **`--trust` needs a terminal.** CI jobs that ran `deploy --trust` must pin `sftp.host_key`
  instead. That setup accepted any key, including an attacker's.
- **New default state location.** Records of new deployments go beside `remote_path`. Existing
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
