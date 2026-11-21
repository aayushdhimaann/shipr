# Security model

## What shipr protects against

| Risk | Mitigation |
|---|---|
| Man-in-the-middle on first or later connections | Host keys are always checked against `~/.ssh/known_hosts`, shipr's own known_hosts and an optional extra file. A pinned `host_key` fingerprint wins over all of them and needs no known_hosts entry. An unknown host is refused, and the error shows its fingerprint. `--trust` only lets shipr *ask*: it shows the fingerprint and saves the key after you confirm in a terminal. Without a terminal, and with `--yes`, it never trusts a key. A changed key, or one that doesn't match the pin, is a hard error. |
| Accepting a MITM key in CI | CI runners start with an empty known_hosts, so trusting on first use there would accept any key. shipr refuses instead and tells you to pin `host_key`. |
| Tampered `sha256sum` output | When the server hashes files for shipr, its output is used only if it has exactly one line per requested file, in order, for exactly that path, with exit status 0. A crafted file name (BusyBox doesn't escape newlines), an unreadable file or a missing binary makes shipr download and hash the files itself. |
| Uploading secrets | Built-in excludes for `.env*`, keys and `.git`. Only *committed* files are deployed, so a stray local file can't slip in. |
| Secrets in config or logs | Credentials come only from environment variables or `.env`. Validation errors never echo values. Passwords and passphrases are kept as secret values that `repr()` and tracebacks don't show. Commands are not interpolated, so a secret never appears in output or deploy history. |
| Build tools reading deploy credentials | By default local commands see your whole environment and `.env`, as documented, so a compromised npm or composer package could read deploy passwords. `build.env` narrows this to the variables you name plus a few basics. Without it, `shipr validate` and every deploy warn and name the variables that supply provider settings (never their values). The next major release will hide those variables unless `build.env` names them. |
| Config mistakes reaching production | Duplicate YAML keys are errors, with line numbers. Provider settings are validated by `shipr validate`, not only on connect. Neither error, nor an invalid YAML error, prints a setting's value or the source line. `default_excludes: false` warns and lists the files it lets through. |
| Writing or deleting outside the deploy folder | Every path is validated *before* anything changes, including paths read back from the server's manifest. `remote_path` and `state_path` must be normalised absolute paths, and `/` is refused. Repo files can never overwrite shipr's state directory. |
| Symlinks on the server redirecting a write, read or delete | Below `remote_path` and `state_path`, shipr never goes through a symlink, special file or unreadable directory. The planner leaves anything at or under one untouched and reports it. Before every mkdir, upload, rename, delete and lock, each directory between the root and the target is checked again with `lstat`. A directory shipr creates is re-checked right after creating it. There is no cache of earlier checks. |
| State mixed with site files or another environment's state | `state_path` may not be `remote_path` or a parent of it. On the same server, environments may not share or nest state dirs, or keep state inside another's `remote_path`. When shipr can't tell whether two hosts are the same machine, it warns. |
| Deleting the only copy of a file after a crash | Leftovers from interrupted uploads are removed only if they belong to a file shipr manages and, for a parked previous copy, match the deployed version or a backup. Otherwise the deploy stops before changing anything. |
| Losing a live file during a non-atomic rename | On servers without atomic `posix-rename`, the old file is moved aside and put back if the swap fails. |
| Looser permissions | A replaced file keeps the server's permissions. New files keep their executable bit but lose group/other write. Backups keep the original mode, so a 0600 config stays 0600. |
| Deleting files that aren't shipr's | Only files recorded in shipr's own manifest can be deleted. Deletion always asks; a non-interactive run needs `--yes`. Deleted files are moved into a backup folder rather than erased. |
| Overwriting hotfixes made on the server | A file whose size or modified time changed since shipr's last deploy is backed up before it is overwritten. |
| Half-written files served to visitors | Each upload goes to a temporary file that is then renamed over the target. |
| Other server users reading a file while it uploads | The temporary file is created with mode 0600 in the SFTP open request itself (O_CREAT\|O_EXCL), and only gets its final mode once complete. A server that ignores the requested mode gets a chmod before the first byte is written. A temp file left by an earlier attempt is deleted and recreated, never reused. Local temp files (downloads, the git export) live in directories only you can read. |
| Losing a deleted file when state is on another filesystem | The server can't move a file across filesystems (OpenSSH answers "Failure"). shipr then copies it into the backup folder (complete or not at all, via a temp file), checks that the copy's SHA-256 matches the original, and only then removes the original. An existing backup is never overwritten. If the copy doesn't match, the original stays. An interruption at any point leaves the original in place. |
| Unsafe archive extraction | The git export is extracted with Python's `data` filter: no absolute or `../` paths, no links out of the export, no device files. Pythons without the filter (3.10.0–3.10.11, 3.11.0–3.11.3) are refused with a clear error; there is no unfiltered fallback. |
| Two deploys at once | An exclusive lock file is created on the server for the length of the deploy. Each lock holds a random token, so a deploy recognises its own lock byte for byte. A deploy removes only its own lock, re-reading it right before the removal, including on a retry after a lost reply. If the lock was removed or taken over while the deploy waited at a prompt, the deploy stops before changing anything. `shipr unlock` shows the holder first and removes only that exact lock (or the one named with `--id`). Locks never expire. `kill` (SIGTERM) is handled like Ctrl-C: progress is saved and the lock released. |
| Deploy records exposed on the website | By default records go beside `remote_path`, in its parent folder (`/var/www/html` → `/var/www/.shipr/html`). That is outside the site only when `remote_path` is the web root itself; see "What you must know". The first deploy to the default location and `shipr test` warn about this. Records inside `remote_path` (configured, or left there by shipr 0.1) warn on every deploy and in `shipr status` and `shipr test`. `.htaccess` is written but not relied on: it only works on Apache with `AllowOverride`. |
| A deploy reported as successful when its records weren't saved | The manifest and the history record are saved separately and checked. If either fails, the deploy exits non-zero, and a manifest that wasn't saved marks the record `failed`. Each record notes whether its files finished, how its hooks went, and whether the manifest was saved. |

## What you must know

- **The deploy lock is not a compare-and-swap.** SFTP has no atomic "delete if unchanged", so
  removing a lock is a read followed by a delete. If someone runs `shipr unlock` on a deploy that
  is still running and a second deploy takes the lock within that one round trip, the first
  deploy's release could remove the second's lock. Only run `shipr unlock` when no deploy is
  running. A running deploy checks its lock before its first change and when it finishes, and
  warns if it was taken over, but not between files.
- **`shipr.yml` is code.** `build.commands` and `hooks` run through a shell on your machine and on
  the server, the same way `package.json` scripts do. Review changes to `shipr.yml` in pull requests
  the same way you review code. Don't run shipr on repositories you don't trust.
- **The deploy user's permissions are the real boundary.** Use a dedicated SSH user that can write
  only to the web root, not root.
- **Prefer key authentication** with a passphrase, or an ssh-agent, over passwords. In CI, keep
  deploy credentials out of build steps' reach with `build.env`, and pin `host_key`.
- **known_hosts writes.** shipr appends each line in one write, so concurrent shipr processes
  don't interleave lines on POSIX. Windows doesn't guarantee this for appends, but a pinned
  `host_key` needs no known_hosts writes at all.
- **Detecting server-side edits uses size and modified time, not file contents** (hashing every
  remote file would mean downloading it). An edit that keeps the same size *and* restores the old
  modified time would go unnoticed.
- **Symlink checks narrow a race; they can't close it.** SFTP works on paths, not handles. Protocol
  version 3 has no `O_NOFOLLOW`, no `openat` and no compare-and-swap. A process that can write to
  your web root can still swap a directory for a symlink between shipr's last `lstat` and the
  request that follows it. shipr checks immediately before each request, so the window is one
  round trip, but the deploy user's permissions remain the real boundary. Don't let the web
  server's user write where shipr deploys, other than in directories you `protect`. Components
  *above* `remote_path` and `state_path` (for example `/var/www -> /srv/www`) are your own
  configuration and are followed.
- **The default state location is not guaranteed to be private.** shipr can't know your web
  server's document root. If `remote_path` is a folder inside a site (`public_html/staging`), the
  default beside it is inside that site too. Set `state_path` to a folder outside the web root
  whenever the default could be served.
- **Hooks may run more than once.** After a failed, interrupted or partial deploy, the next deploy
  re-runs that phase's hooks, including ones that had already succeeded. Make hooks safe to
  repeat.
- **Backups and the manifest may contain your code.** Keep `state_path` private (see above) and
  prune old backups.

Report vulnerabilities as described in [SECURITY.md](../SECURITY.md).
