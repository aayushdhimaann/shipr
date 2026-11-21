# Security model

## What shipr protects against

| Risk | Mitigation |
|---|---|
| Man-in-the-middle on first or later connections | Host keys are always checked against `~/.ssh/known_hosts` and shipr's own known_hosts. An unknown host is refused until you run `shipr test --trust`, which shows the fingerprint and saves it. A changed key is a hard error. |
| Uploading secrets | Built-in excludes for `.env*`, keys and `.git`. Only *committed* files are deployed, so a stray local file can't slip in. |
| Secrets in config or logs | Credentials come only from environment variables or `.env`. Validation errors never echo values. Commands are not interpolated, so a secret never appears in output or deploy history. |
| Writing or deleting outside the deploy folder | Every path is validated *before* anything changes, including paths read back from the server's manifest. `remote_path` and `state_path` must be normalised absolute paths, and `/` is refused. Repo files can never overwrite shipr's state directory. |
| Symlinks on the server redirecting a write, read or delete | Below `remote_path` and `state_path`, shipr never goes through a symlink, special file or unreadable directory. The planner leaves anything at or under one untouched and reports it. Before every mkdir, upload, rename, delete and lock, each directory between the root and the target is checked again with `lstat`. A directory shipr creates is re-checked right after creating it. There is no cache of earlier checks. |
| State mixed with site files or another environment's state | `state_path` may not be `remote_path` or a parent of it. On the same server, environments may not share or nest state dirs, or keep state inside another's `remote_path`. When shipr can't tell whether two hosts are the same machine, it warns. |
| Deleting the only copy of a file after a crash | Leftovers from interrupted uploads are removed only if they belong to a file shipr manages and, for a parked previous copy, match the deployed version or a backup. Otherwise the deploy stops before changing anything. |
| Losing a live file during a non-atomic rename | On servers without atomic `posix-rename`, the old file is moved aside and put back if the swap fails. |
| Looser permissions | A replaced file keeps the server's permissions. New files keep their executable bit but lose group/other write. Backups keep the original mode, so a 0600 config stays 0600. |
| Deleting files that aren't shipr's | Only files recorded in shipr's own manifest can be deleted. Deletion always asks; a non-interactive run needs `--yes`. Deleted files are moved into a backup folder rather than erased. |
| Overwriting hotfixes made on the server | A file whose size or modified time changed since shipr's last deploy is backed up before it is overwritten. |
| Half-written files served to visitors | Each upload goes to a temporary file that is then renamed over the target. |
| Two deploys at once | An exclusive lock file is created on the server for the length of the deploy. A deploy only ever removes its own lock. `kill` (SIGTERM) is handled like Ctrl-C: progress is saved and the lock released. |
| Deploy records exposed on the website | A `.htaccess` deny is written for Apache, `shipr test` warns, and `state_path` can be moved outside the web root. |

## What you must know

- **`shipr.yml` is code.** `build.commands` and `hooks` run through a shell on your machine and on
  the server, the same way `package.json` scripts do. Review changes to `shipr.yml` in pull requests
  the same way you review code. Don't run shipr on repositories you don't trust.
- **The deploy user's permissions are the real boundary.** Use a dedicated SSH user that can write
  only to the web root, not root.
- **Prefer key authentication** with a passphrase, or an ssh-agent, over passwords.
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
- **Backups and the manifest may contain your code.** Keep `state_path` private (see above) and
  prune old backups.

Report vulnerabilities as described in [SECURITY.md](../SECURITY.md).
