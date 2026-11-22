# shipr — Git-aware SFTP deployment CLI (Plan)

> Original design notes, kept for background. The README, [security.md](security.md) and
> [FLOW.md](FLOW.md) describe how shipr behaves today; where they differ, they are right.

## Context
Projects (PHP, React, Node, Next.js, static) are deployed by hand with FileZilla over SFTP: slow and
easy to get wrong. **shipr** is an open-source, framework-agnostic Python CLI that deploys an exact Git
commit to remote servers through pluggable providers (SFTP first). It's safe by default: dry-run,
locking, backups, path guards, and deletion only after you confirm.


## Requirements captured (Phase 1)
| Topic | Decision |
|---|---|
| Server access | Mixed. If SSH exec works, remote pre/post commands run. On SFTP-only hosts, files are uploaded and commands are skipped with a warning (`ssh_exec: auto/true/false`) |
| Builds | Per project/env: local build + upload output, or upload source + remote commands |
| Remote drift | Manual hotfixes happen. A remote file changed since the last deploy is **downloaded to backup, then overwritten**, and the summary reports it |
| Deletes/renames | **Always ask.** Only files shipr itself deployed are offered for deletion. Non-interactive runs need `--yes` |
| Rollback | **Git-based:** redeploy the previously deployed commit, so only the files that differ change back |
| Stack | Python + **uv** |
| Config | Committed `shipr.yml` per project + optional `~/.config/shipr/config.yml` defaults |
| Secrets | Env vars / project `.env` via `${VAR}` interpolation. Never stored by shipr |
| Dirty tree | Deploy committed content only, with a warning about uncommitted changes |
| First deploy on existing site | Compare remote with local, upload only differences, back up overwritten files, record a baseline |
| Large dependency folders | Not needed. Uploads run sequentially in v1 |
| Audience | Open source: Linux/macOS/Windows, teams |

## Key architectural decision: content manifest, with Git as the version source
`git diff` alone can't drive uploads. Build output (`dist/`, `.next/`) isn't tracked in Git, and the
remote may not match any commit because of hotfixes or FileZilla history. So:

1. **Git** picks the commit, exports a clean tree (`git -c core.autocrlf=false archive`, extracted
   with tarfile's safe `data` filter into a temp dir), and records the commit hash and branch in
   history.
2. A build runs inside that temp tree if one is configured.
3. Include/exclude rules are applied, then each file is sha256-hashed. This gives the **local manifest**.
4. The **remote manifest** (`.shipr/manifest.json`) records each deployed file's hash, size and mtime
   *as observed on the remote after upload*.
5. Plan = diff of the two manifests, plus a remote `stat` pass:
   - upload: new or changed hash
   - backup + upload: remote size or mtime differs from the manifest (a hotfix)
   - delete candidates: in the remote manifest but not in the local one. Ask before deleting.
   - Renames come out as delete + upload. Git's `-M` rename detection is shown only as a hint.

The same diff covers every case: build and no-build projects, branch switches, partial or interrupted
deploys (a re-run picks up whatever is left), and rollback (`deploy --ref <previous commit>`).

## How shipr tracks uploaded files
Remote state dir (`state_path`, default `<remote_path>/.shipr/`), one per environment:
```
.shipr/
  manifest.json          # current truth: what shipr deployed and how the server reported it
  lock                   # exclusive deploy lock
  history/<deploy-id>.json   # full record: commit, branch, user, times, status, per-file results
  backups/<deploy-id>/…      # remote copies taken before overwrite/delete
```
`manifest.json`:
```json
{ "version": 1, "environment": "production", "deployed_commit": "a1b2c3d",
  "deploy_id": "20261119T103205Z-a1b2c3d",
  "files": { "index.php": {"sha256": "9f2e…", "size": 2048, "mtime": 1763548325} } }
```
`sha256` is the hash of the content uploaded. `size`/`mtime` are what the server reported in a `stat`
right after upload. Remote files are later checked with a `stat` (one directory listing per folder),
never re-hashed.

Per path, compare **L** (local hash now), **M** (manifest entry) and **R** (remote stat now):
| Situation | Meaning | Action |
|---|---|---|
| L = M.hash, R = M.stat | unchanged | skip |
| L ≠ M.hash, R = M.stat | changed locally | upload |
| R ≠ M.stat (exists) | hotfixed on server | backup → upload, flagged in report |
| M present, R missing | deleted on server | re-upload |
| L present, no M, R exists | file not deployed by shipr (first deploy) | compare hash → backup+upload only if different |
| L present, no M, no R | new file | upload |
| M present, no L | removed/renamed locally | ask before delete (backup first) |
| no L, no M | not shipr's file | never touched |
The manifest lives on the server, so every teammate and machine sees the same state.

## Architecture
```
cli.py ──► engine.py (deploy/rollback)      admin.py (status/history/unlock/test)
             ├─ workspace.py  ref → git archive export → hooks/build → files (uses gitops, files)
             ├─ planner.py    L/M/R comparison → Plan; path validation (pure, no I/O)
             ├─ executor.py   apply Plan; manifest + record updated per file, checkpoints
             ├─ hooks.py      local/remote commands, ssh_exec policy
             ├─ state.py      remote .shipr/: manifest, lock, history, backups
             ├─ models.py     Manifest / DeployRecord (pydantic, validated on load)
             ├─ config.py     schema, global+project+env deep-merge, ${VAR}/.env
             └─ providers/
                  base.py   Provider ABC + errors re-export; registry via entry points
                  sftp.py   paramiko
                  local.py  filesystem target (real provider + test double)
```
**Provider protocol (minimal):** `connect/close`, `stat`, `walk(root)`, `upload(local, remote)`
(atomic: temp name → `posix_rename`), `download`, `delete`, `mkdirs`, `read_text/write_text`,
`create_exclusive` (lock), `exec(cmd)` with a `can_exec` capability flag. FTP, rsync and Docker
providers can later ship as separate packages through entry points, with no changes to the core.

**Deploy flow:** load+validate config → resolve ref (warn if not descendant of deployed commit;
confirm for `confirm: true` envs) → export commit → local hooks/build → collect+hash → connect
(retry w/ backoff) → acquire remote lock → load manifest (warn if a deploy was interrupted) →
plan → show summary (dry-run stops here) → confirm → remote_before (if exec) → backups →
uploads (manifest checkpointed every 25 files) →
confirmed deletions → remote_after (if exec) → write manifest + history record → release lock →
report (status: success | partial | failed, file-level results).

## Config schema (`shipr.yml`)
```yaml
version: 1
project: { name: my-app }
build:
  commands: ["npm ci", "npm run build"]   # omit for no-build projects
  output: dist                            # deploy root inside repo (default ".")
files:
  include: ["**"]
  exclude: ["*.map"]                      # added to built-in secret/junk excludes
  protect: ["uploads/**", "config.php"]   # never overwritten or deleted remotely
hooks:
  local_before: []      local_after: []
  remote_before: []     remote_after: ["php artisan migrate --force"]
defaults:                                 # merged into every environment
  provider: sftp
  sftp: { port: 22, timeout: 15, retries: 3 }
environments:
  production:
    branch: main
    confirm: true
    remote_path: /var/www/html
    state_path: /home/deploy/.shipr/my-app  # optional; default <remote_path>/.shipr (+ deny .htaccess)
    ssh_exec: auto
    sftp: { host: example.com, username: deploy, key_file: ~/.ssh/id_ed25519,
            passphrase: "${SHIPR_PROD_KEY_PASS}" }   # or password: "${SHIPR_PROD_PASSWORD}"
```
Built-in excludes: `.git/`, `.shipr/`, `.env*`, `*.pem`, `*.key`, `id_rsa*`, `shipr.yml`,
`node_modules/`, `.DS_Store`, `*.log`. They can be turned off with `files.default_excludes: false`.
A top-level `source:` (project folder in the repo) sits next to `build.output` (relative to source).
`remote_path` must be absolute (`~` isn't expanded over SFTP; `shipr test` prints the login dir).

## CLI
`shipr init` (template; looks at package.json/composer.json only to prefill values) · `shipr validate` ·
`shipr test -e ENV` (connect, host key, write permission, exec check, warns if state is in the webroot) ·
`shipr deploy -e ENV [--ref R] [--dry-run] [--yes] [--no-delete]` · `shipr status -e ENV` (deployed
commit, drift) · `shipr history -e ENV [--show ID]` · `shipr rollback -e ENV [--to ID]` · `shipr unlock -e ENV`.
I dropped `configure`: the config is YAML and secrets come from env vars, so `init` + editing covers it.

## Safety & security model
- **Host keys:** checked against known_hosts. `shipr test --trust` does trust-on-first-use. Never auto-accept silently.
- **Paths:** remote paths are normalized, and `..`, absolute paths and anything outside `remote_path` are rejected. A `remote_path` of `/`, `~` or empty is refused.
- **Atomic uploads:** temp file then rename, so the web server never serves a half-written file. Interrupted runs leave only `*.shipr-tmp` files, which are cleaned up on the next run.
- **Lock:** `create_exclusive` on `.shipr/lock` (user, host, time). Stale locks are reported and released with `shipr unlock`.
- **Checkpointed manifest** instead of a separate journal: progress is saved every 25 files with an
  `in_progress` marker, so an interrupted deploy is reported and completed by the next run.
- **Secrets:** read only from env/.env, redacted from logs. The config is trusted code: hooks run in a shell, as with npm scripts. This goes in SECURITY.md.
- **State exposure:** the manifest and backups sit in `.shipr/`. If that's inside the webroot, shipr writes a deny `.htaccess` and `test` warns. Docs recommend `state_path` outside the webroot.
- **Protected globs** are never touched. Production asks for confirmation. Dry-run is the same code path with the executor disabled.

## Edge cases tracked
CRLF conversion on Windows exports · symlinks (not followed out of the tree) · empty dirs (only remove
dirs shipr created and that are now empty) · file modes (configurable defaults 644/755) · case-insensitive
remote FS · unicode names · submodules/LFS (v1: warn, not exported) · disk full / permission denied →
partial status · opcache/stale caches → remote_after hook · mtime-only hotfix detection limit (a same-size
edit with mtime preserved would be missed; documented).

## Dependencies
paramiko, click, pydantic (v2), pyyaml, pathspec, python-dotenv. Git is called through the CLI, not GitPython.
Dev: pytest, ruff. License: **MIT**.

## Repo layout
`pyproject.toml` (uv, hatchling, `shipr = shipr.cli:main`) · `src/shipr/…` (above) · `tests/unit/` ·
`tests/integration/` (Docker `linuxserver/openssh-server`, with exec and SFTP-only variants) ·
`examples/{php,react,node,nextjs,static}/shipr.yml` · `docs/{configuration,security,providers}.md` ·
README, CONTRIBUTING, SECURITY.md, CHANGELOG, LICENSE, `.gitignore`, `.github/workflows/ci.yml`
(ruff + pytest on ubuntu/macos/windows).

## Milestones (each one: code + tests + docs + suggested commit message)
1. **M0 Scaffold:** `git init`, uv project, license, README skeleton, CI, `.gitignore`.
2. **M1 Config + selection:** schema/merge/env interpolation, `init`, `validate`, gitops export, file
   collection/hashing, local-only `deploy --dry-run` listing.
3. **M2 Engine on LocalProvider:** planner, manifest, backups, delete confirmation, lock, checkpoints,
   history, `status`/`history`/`unlock`. Full end-to-end deploys to a local folder.
4. **M3 SFTP provider:** password/key/passphrase, known_hosts/TOFU, timeout+retry, atomic upload,
   exclusive lock, exec detection, remote hooks, `test`. Docker integration tests.
5. **M4 Build, baseline & rollback:** local build in the export tree, first-deploy remote compare
   (sha256sum over exec when available, otherwise download+hash), `rollback`.
6. **M5 Validation & docs:** sample PHP/React/Node/Next.js/static projects in a temp dir, deployed to
   Docker SFTP. Full docs and examples.
- Later: git hooks/webhooks, parallel uploads, FTP/rsync/Docker providers, notifications.

## Verification (Phase 5)
- Unit: config merge/validation, glob rules, planner decisions (upload/backup/delete/protect), path guards.
- Integration (Docker SFTP): password and key+passphrase auth, wrong credentials, unreachable host + retry,
  host key mismatch, full deploy, incremental deploy, deletion prompt and `--no-delete`, rename, hotfix
  backup, exclusions, lock contention, SFTP-only server (hooks skipped), a process killed mid-upload and
  then re-run, rollback.
- Project matrix: deploy each of the 5 sample project types and check the remote tree against expectations.
- `uv run pytest` and `uv run ruff check` must pass before each milestone is reported done. Nothing is reported as working without a test run behind it.

## Implementation status (2026-11-19)
| Milestone | Status |
|---|---|
| M0 Scaffold | done (uv project, MIT, CI for Linux/macOS/Windows, .gitignore) |
| M1 Config + selection | done |
| M2 Engine on LocalProvider | done |
| M3 SFTP provider | done: tested against Docker OpenSSH (full shell) and atmoz/sftp (SFTP-only) |
| M4 Build, baseline & rollback | done |
| M5 Validation & docs | docs and examples done; real-project CLI matrix pending (needs commits in sample repos) |
