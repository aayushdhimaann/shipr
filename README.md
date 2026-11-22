# shipr

**Deploy an exact Git commit to your servers over SFTP, without FileZilla and without surprises.**

shipr is a small, framework-agnostic command-line tool. You describe a project once in a
committed `shipr.yml`. `shipr deploy` then:

1. exports the commit,
2. runs your build,
3. uploads only what changed,
4. asks before deleting anything,
5. records who deployed what, and when.

```console
$ shipr deploy -e production
my-app → production: main @ 3f2a9c1 "Fix checkout total"
$ npm ci
$ npm run build
212 deployable files
  upload  assets/index-8f3a.js  (new)
  upload  index.html  (changed)
  upload  config.php  (changed on server since last deploy, backup first)
  delete  assets/index-1b2c.js  (renamed to assets/index-8f3a.js)
3 to upload, 1 to delete, 208 unchanged, 0 protected
Deploy 3f2a9c1 to PRODUCTION (sftp://deploy@example.com:22/var/www/html)? [y/N]: y
Delete these 1 files from the server (they are backed up first)? [y/N]: y
success: 3 uploaded, 1 deleted, 0 failed in 2.4s (deploy id 20261119T103205123Z-3f2a9c1)
```

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Commands](#commands)
- [Common workflows](#common-workflows)
- [How it works](#how-it-works)
- [Safety](#safety)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Upgrading from 0.1](#upgrading-from-01)
- [Development](#development)
- [Contributing, security, license](#contributing-security-license)

## Features

- **Any stack.** PHP, React, Vue, Node.js, Next.js, static sites, or anything else. Builds are plain
  shell commands; shipr has no framework-specific code.
- **Deploys commits, not your working folder.** Uncommitted edits never reach the server, and every
  deploy maps to a commit hash.
- **Incremental.** Only changed files are uploaded, even for build output that isn't in Git.
- **Safe by default.** Dry-run, confirmation for production, and a prompt before any deletion. Files
  edited on the server are backed up before they are overwritten. Secrets are never uploaded.
  Uploads are atomic, a lock prevents two deploys at once, and interrupted deploys resume.
- **Works on cheap shared hosting.** It needs only SFTP. If the server also gives SSH shell access,
  remote commands can run before or after the upload (`composer install`, `pm2 reload`,
  migrations).
- **History and rollback.** Every deploy is recorded on the server, and you can roll back to an
  earlier one.
- **Pluggable.** SFTP is the first provider. The core is written against a small provider
  interface, so FTP, rsync and others can be added as plugins.

## Requirements

| | |
|---|---|
| Your machine | Python 3.10.12+, 3.11.4+ or 3.12+, and `git`, on Linux, macOS or Windows. Older patch releases lack the safe archive extraction shipr relies on, and are refused. |
| The server | SFTP access. SSH shell access is optional and only needed for remote hooks. |
| For builds | Whatever your build commands need (Node.js for `npm run build`, Composer, ...) |

## Installation

shipr is not on PyPI yet. Install it from GitHub, pinned to a release tag, or from a wheel
built from a release:

```bash
# with uv (recommended): installs `shipr` as a global command in its own environment
uv tool install git+https://github.com/aayushdhimaann/shipr@v0.2.0

# or with pipx
pipx install git+https://github.com/aayushdhimaann/shipr@v0.2.0

# or from a downloaded or locally built wheel
uv tool install ./shipr-0.2.0-py3-none-any.whl

# or from a local clone
git clone https://github.com/aayushdhimaann/shipr && cd shipr
uv tool install .
```

uv and pipx pick a Python that matches shipr's requirement (3.10.12+, 3.11.4+ or 3.12+). On an
older patch release shipr refuses to run and says why.

Check that it worked:

```bash
shipr --version
```

To upgrade, run `uv tool upgrade shipr` (or `pipx upgrade shipr`). To remove it, run
`uv tool uninstall shipr`.

## Quick start

```bash
cd my-project                       # any Git repository

shipr init                          # 1. write a commented shipr.yml (detects npm/composer builds)
$EDITOR shipr.yml                   # 2. set host, username and remote_path for each environment

echo ".env" >> .gitignore           # 3. only if you use a password: keep it out of Git
echo "SHIPR_PASSWORD=..." >> .env

git add shipr.yml .gitignore
git commit -m "Add shipr config"    # 4. shipr deploys committed content only

shipr validate                      # 5. check the config
shipr test -e staging --trust       # 6. verify the server key, write access, shell access
shipr deploy -e staging --dry-run   # 7. preview
shipr deploy -e staging             # 8. deploy
```

## Configuration

### Where settings come from

`shipr.yml` lives in your project. Commit it, and keep secrets out of it.

shipr finds the file by searching the current directory and then each parent; you can also pass
`-c path/to/shipr.yml`. For the environment you deploy to, settings are merged from these layers.
Later layers win:

1. `defaults:` in the **global** file:
   - Linux: `~/.config/shipr/config.yml`
   - macOS: `~/Library/Application Support/shipr/config.yml`
   - Windows: `%APPDATA%\shipr\config.yml`
2. The project's top-level `source`, `build`, `files` and `hooks`.
3. The project's `defaults:`.
4. The environment's own block under `environments:`.

Mappings are merged key by key. A list replaces the earlier list rather than being added to it.

### Minimal example

```yaml
version: 1
project: {name: my-react-app}
build:
  commands: ["npm ci", "npm run build"]
  output: dist
environments:
  production:
    branch: main
    confirm: true
    remote_path: /var/www/html
    sftp: {host: example.com, username: deploy, key_file: ~/.ssh/id_ed25519}
```

### Full reference

```yaml
version: 1                       # required format version

project:
  name: my-app                   # shown in output and history (default: folder name)

source: .                        # project folder inside the repo; build commands run here

build:
  commands: []                   # shell commands, run in order inside the exported commit
  output: .                      # folder (relative to source) whose contents are uploaded
  env: null                      # list of variable names local commands may see (plus PATH,
                                 # HOME and other basics); null = all of them, with a warning
                                 # (see "Secrets")

files:
  include: ["**"]                # gitignore-style patterns, relative to the output folder
  exclude: []                    # added to the built-in excludes (see below)
  protect: []                    # never overwritten or deleted once they exist on the server
  default_excludes: true         # false disables the built-in excludes (careful! warns and
                                 # lists the files they would have kept back)

hooks:
  local_before: []               # run locally before the build (also during --dry-run)
  local_after: []                # run locally after a successful deploy that changed something
  remote_before: []              # run on the server, in remote_path, before uploading
  remote_after: []               # run on the server, in remote_path, after a fully successful upload
                                 # hooks can run more than once: make them safe to repeat (below)
  timeout: 1800                  # seconds each hook command may run (null = no limit)

defaults: {}                     # any environment key below, applied to every environment

environments:
  production:
    provider: sftp               # sftp | local | a plugin
    branch: main                 # what `shipr deploy` deploys by default (default: current HEAD)
    remote_path: /var/www/html   # absolute, normalised path on the server; "/" is refused
    state_path: null             # where shipr keeps its records (default: beside the web
                                 # root, /var/www/html -> /var/www/.shipr/html); a directory of
                                 # its own: not remote_path or a parent of it
    confirm: false               # ask before deploying (use for production)
    ssh_exec: auto               # auto | true | false: run hooks over SSH?
    sftp:
      host: example.com
      port: 22
      username: deploy
      password: "${SHIPR_PASSWORD}"     # password auth, or:
      key_file: ~/.ssh/id_ed25519       # key auth (RSA / ECDSA / Ed25519)
      passphrase: "${SHIPR_KEY_PASS}"   # if the key is encrypted
      timeout: 15                       # seconds, for connecting and for each operation
      retries: 3                        # reconnect attempts on network errors (with backoff)
      known_hosts: null                 # extra known_hosts file to trust
      host_key: null                    # pinned server key, e.g. "SHA256:abc..." (or a list
                                        # while rotating keys); wins over known_hosts
```

- If there is no `password`, shipr also tries your ssh-agent. You can leave out both `password`
  and `key_file` and rely on the agent alone.
- `ssh_exec` decides whether remote hooks run:
  - `false` deliberately turns remote hooks off. shipr never probes, and says it skipped them.
    Use it for SFTP-only hosting.
  - `true` makes them required: a server without a shell fails the deploy.
  - `auto` (the default) probes once. A working shell runs the hooks. A server that clearly
    refuses (OpenSSH's "This service allows sftp connections only.", a nologin shell) skips
    them with a warning. A check that times out or loses its connection proves nothing, so with
    remote hooks configured the deploy stops before changing anything, rather than silently
    skipping a migration.
- The `local` provider deploys to a folder on this machine, such as a mounted share or for tests.
  It takes `local: {root: /mnt/share}`, and `remote_path` is then resolved inside `root`.

### Built-in excludes

These are never uploaded unless you set `default_excludes: false`. With it, `shipr validate` and
every deploy warn and list the files these patterns would have kept back (names only):

```
.git/  .shipr/  .env  .env.*  *.pem  *.key  id_rsa*  id_ed25519*  shipr.yml
node_modules/  .DS_Store  Thumbs.db  *.log  *.shipr-tmp  *.shipr-old
```

Files marked `export-ignore` in `.gitattributes` are left out as well.

### Secrets and variables

Any setting can contain `${NAME}`. shipr takes the value from the process environment (your
shell, or your CI's secrets). If the variable isn't set there, it reads the `.env` file next to
`shipr.yml`. A variable that is used but missing is an error. `.env` values are read literally,
with no `${...}` expansion inside them.

**Commands are never interpolated.** `build.commands` and `hooks` run exactly as written, so a
secret never shows up in output or in deploy history. Inside a command, use shell syntax
(`$TOKEN`):

- **Local commands** (build, `local_*`) get your environment, the values from `.env`, and
  `SHIPR_ENV`, `SHIPR_COMMIT` and `SHIPR_PROJECT`. That includes your deploy credentials, so
  every package an `npm ci` or `composer install` runs could read them. To limit this, list
  the variables commands need in `build.env`, for example `env: [NODE_ENV, VITE_API_URL]`.
  Commands then get only those, a few basics (`PATH`, `HOME`, locale, temp dirs and the Windows
  system variables) and the `SHIPR_*` variables above. Without `build.env`, `shipr validate`
  and every deploy warn, naming the variables that feed your provider settings (never their
  values). An ssh-agent socket (`SSH_AUTH_SOCK`) is part of your environment too, and
  `build.env` keeps it out.
- **Planned for the next major release:** variables that supply provider settings (such as
  `password: ${SHIPR_PASSWORD}`) will be hidden from local commands unless `build.env` names
  them. Set `build.env` now to get that behaviour today.
- **Remote commands** (`remote_*`) run with the server's own environment.

### Recipes by project type

Complete files are in [`examples/`](examples).

| Project | What to configure |
|---|---|
| **Static site** | Nothing to build. If the site lives in a subfolder, set `source: public`. → [example](examples/static/shipr.yml) |
| **PHP** | No build. Set `protect: ["uploads/**", "config.php"]`. Run `composer install` locally as a build command, or on the server in `remote_after`. → [example](examples/php/shipr.yml) |
| **React / Vue (Vite, CRA)** | `build: {commands: ["npm ci", "npm run build"], output: dist}`. Create React App writes to `build`. → [example](examples/react/shipr.yml) |
| **Node.js API** | Upload the source, then `remote_after: ["npm ci --omit=dev", "pm2 reload app"]`. → [example](examples/node/shipr.yml) |
| **Next.js** | For a static export, build locally with `output: out`. For server mode, upload the source and build in `remote_after`. → [example](examples/nextjs/shipr.yml) |
| **Monorepo** | Put `shipr.yml` in the app's folder, or set `source: apps/web`. |

### Where shipr keeps its records

Everything lives in `state_path` on the server, so every teammate sees the same state:

```
.shipr/manifest.json            what is deployed: each file's hash, size and time, plus the commit
.shipr/lock                     present while a deploy runs
.shipr/history/<id>.json        one record per deploy (who, when, commit, every file)
.shipr/backups/<id>/...         server copies saved before overwriting or deleting
```

By default these live **beside `remote_path`**, in its parent folder: `remote_path:
/var/www/html` keeps its records in `/var/www/.shipr/html`, and `/home/u123/public_html` keeps
them in `/home/u123/.shipr/public_html`. If the deploy user can't create that directory, the
deploy stops before changing anything and tells you to set `state_path`. Backups are never
deleted automatically.

**The default is not private by itself.** It is outside your site only when `remote_path` *is*
the web root. If you deploy into a folder inside a site, for example `remote_path:
/home/u123/public_html/staging`, the default (`/home/u123/public_html/.shipr/staging`) is inside
that site too, and anyone could fetch it over HTTP unless the web server denies it. shipr can't
know your web server's document root, so it doesn't guess. It warns the first time it creates
records at the default location and in `shipr test`, and `shipr status` labels the location.
Whenever the default could be served, set an explicit `state_path` outside the web root, for
example `/home/deploy/.shipr/my-app-staging`.

**Records inside the web root** (`state_path` under `remote_path`, or records in the legacy
0.1 layout at `<remote_path>/.shipr`) still work, but shipr warns on every deploy. Anyone who can
reach that path over HTTP can read the manifest, history and backups, and backed-up scripts may
still run. A `.htaccess` deny is written, but it only works on Apache with `AllowOverride`. On
nginx, add `location ^~ /.shipr/ { deny all; }`.

**Moving legacy 0.1 records out of the web root.** "0.1" here names the old records layout
(`<remote_path>/.shipr`), which earlier development builds used by default; it is not a package
release. With no `state_path` set, shipr keeps
using `<remote_path>/.shipr` until records appear at the new default. shipr never moves, copies or
deletes them itself, so their history stays intact. To move them:

1. Make sure no deploy is running.
2. Copy `<remote_path>/.shipr` to the new default location, or to a `state_path` you set.
3. Check the copy, then delete `<remote_path>/.shipr`.

If you stop between steps 2 and 3, the records exist in both places. shipr then refuses to deploy
and names both locations, instead of guessing which is current. A copy that has no
`manifest.json` yet is ignored, and the original stays in use. Records that belong to a different
`remote_path` are always refused.

`state_path` must be a directory of its own. It may not be `remote_path` or one of its parents.
Two environments on the same server may not share state, nest their state dirs, or keep state
inside each other's `remote_path`. `shipr validate` rejects these setups. When shipr can't tell
whether two hosts are the same server, it warns instead.

## Commands

Every command accepts `-c/--config FILE`. `-e/--env NAME` picks the environment; you can leave it
out when `shipr.yml` defines only one.

### `shipr init`
Writes a commented `shipr.yml` in the current folder. It looks at `package.json` or
`composer.json` only to pre-fill a sensible build step.

| Option | |
|---|---|
| `--force` | Overwrite an existing `shipr.yml` |

### `shipr validate`
Loads and checks the configuration of every environment: unknown or duplicate keys, invalid
paths, missing `${VARIABLES}` and the provider's own settings (for SFTP: misspelled keys, missing
`host` or `username`, malformed `host_key`). It also warns about build commands that can read
deploy credentials and about `default_excludes: false`. It does not connect to anything.

### `shipr test -e ENV`
Connects to the server and reports:
- the host key fingerprint and your login directory
- write access to the state directory
- how many files are in `remote_path`
- whether shell commands are available

| Option | |
|---|---|
| `--trust` | For an unknown host key: show its fingerprint, ask you to confirm it (terminal only; never with `--yes`), then remember it in shipr's `known_hosts` (next to the global config) |

### `shipr deploy -e ENV`
Exports the commit, builds, plans, asks, uploads and records.

| Option | |
|---|---|
| `--ref REF` | Branch, tag or commit to deploy (default: the environment's `branch`, else `HEAD`) |
| `-n, --dry-run` | Show the plan and change nothing (no lock, no writes) |
| `-y, --yes` | Answer yes to the production confirmation and to deletions. Required when not in a terminal. |
| `--no-delete` | Never delete remote files in this run; they stay on the server and are offered again next time. If keeping them is all that's left to do, the deploy is "up to date": no hooks run and nothing is recorded |
| `--trust` | As for `test` |

```bash
shipr deploy -e staging                   # deploy the env's branch (or HEAD)
shipr deploy -e production --dry-run      # preview only
shipr deploy -e staging --ref v1.4.2      # a tag
shipr deploy -e staging --ref 3f2a9c1     # a specific commit
shipr deploy -e production --yes          # non-interactive (CI)
```

### `shipr status -e ENV`
Shows the deployed commit and branch, whether your branch has moved since, any interrupted deploy
or held lock, and **drift**: files changed or deleted on the server since the last deploy.

### `shipr history -e ENV`
Lists past deploys: id, kind, status, commit, branch, number of files uploaded (↑), deleted (✗)
and failed (!), and who ran it.

| Option | |
|---|---|
| `-n N` | How many recent deploys to list (default 20) |
| `--show ID` | Print one deploy record in full (JSON, including every file) |

### Hooks run again after a failure

When a remote hook fails, or a deploy is interrupted or ends `partial`, its hooks haven't all
finished. The next deploy runs them again, even if no file changed. It runs *every* hook of that
phase again, including any that had already succeeded, because shipr doesn't track hooks one by
one. Write hooks so that running them twice is safe: `npm ci`, `composer install`, cache clears,
`php artisan migrate` (it skips applied migrations) and `pm2 reload` are fine. A hook that sends
an email or appends to a file on every run is not.

### Hook output and timeouts

Remote hooks show their output as it arrives, with stderr merged into stdout in order. Each
hook command may run for `hooks.timeout` seconds (default 1800; `null` means no limit). A
command that runs longer fails the deploy, is **not** retried in that run, and runs again on the
next deploy (see above). Two limits apply:

- **A timed-out remote command may still be running on the server.** SSH lets shipr stop
  waiting and close the channel, but not reliably stop the command. Check the server before
  deploying again, especially for migrations.
- **Local hooks** (`local_before`, `local_after`, and remote hooks on the `local` provider) run
  without a terminal for input, in their own process group. On timeout or Ctrl-C, shipr stops
  the command *and the processes it started*. Build commands (`build.commands`) have no timeout
  and run exactly as in your terminal.

### `shipr rollback -e ENV`
Redeploys an earlier commit through the normal pipeline, so only the files that differ change
back. Repeated rollbacks keep stepping further back. The commit must exist in your local repo
(`git fetch` first).

The target comes from the deploy history: every deploy whose files reached the server counts,
even if a hook failed afterwards. If the last deploy was interrupted, rollback restores the last
one that completed. If history and the manifest disagree about what is deployed (for example the
manifest couldn't be saved), rollback refuses to guess and asks for `--to`.

| Option | |
|---|---|
| `--to ID\|COMMIT` | A deploy id, or a commit prefix of at least 4 characters, from `shipr history` (default: the previous deploy). A prefix that matches more than one deployed commit is refused, with the candidates listed. |
| `-n`, `-y`, `--no-delete` | As for `deploy` |

### `shipr unlock -e ENV`
Removes the deploy lock left by a deploy that was killed hard (power loss, `kill -9`) or lost its
connection for good. Use it only when no deploy is running. It shows which deploy holds the lock
(deploy id, user and machine, and how long ago it started) and asks. It removes only that exact
lock: if another deploy took the lock in the meantime, nothing is removed. Locks never expire on
their own, however old: shipr can't tell a slow deploy from a dead one.

| Option | |
|---|---|
| `--id DEPLOY_ID` | Remove the lock only if that deploy holds it, without a prompt (for scripts). The "another deploy holds the lock" error prints the exact command. |
| `-y, --yes` | Not enough on its own: shipr never removes a lock nobody has seen. It shows the holder and the `--id` command instead (exit code 3). |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success, nothing to do, or dry run |
| `1` | Error: config, git, connection, lock, or a command that failed |
| `2` | Wrong command-line usage |
| `3` | Cancelled at a confirmation |
| `4` | Partial deploy: some files failed. Run deploy again to retry them. |
| `130` | Interrupted by Ctrl-C or `kill`. Progress was saved; run the command again. |

## Common workflows

**First deploy to a site that already exists** (uploaded earlier with FileZilla): run a normal
deploy. Files that are already identical are adopted without uploading, and differing files are
backed up and replaced. Files on the server that aren't in your project are left alone.

**Everyday deploy:** commit, then run `shipr deploy -e staging`. Check the result, then run
`shipr deploy -e production`.

**Someone hot-fixed a file on the server:** `shipr status` lists it as drift. The next deploy backs
it up to `.shipr/backups/<id>/` before overwriting it, and the summary says so. Copy the fix into
your repo if you want to keep it.

**Roll back:** run `shipr history -e production`, then `shipr rollback -e production` for the
previous deploy, or add `--to <id>` for a specific one.

**A deploy was interrupted** (network drop, Ctrl-C, `kill`): run the same deploy again. It reports
the interruption and uploads only what is still missing. After a hard kill (`kill -9`, power
loss), or a network that never came back during the deploy, the lock is still on the server.
shipr won't guess that its holder is dead, so check that no deploy is running, then run
the `shipr unlock -e ENV --id <deploy id>` command the error prints.

**Deploy from CI** (GitHub Actions example):

```yaml
- run: uv tool install git+https://github.com/aayushdhimaann/shipr@v0.2.0
- run: shipr deploy -e production --yes
  env:
    SHIPR_PROD_PASSWORD: ${{ secrets.SHIPR_PROD_PASSWORD }}
```

**Check the host key in CI by pinning it.** A CI runner starts with no `known_hosts`, so pin the
server's verified fingerprint in `shipr.yml`:

```yaml
    sftp:
      host: example.com
      host_key: "SHA256:REPLACE-with-your-servers-verified-fingerprint"   # 43 chars after SHA256:
```

Get it from your hosting provider, or run `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on
the server, or read the `host key:` line that `shipr test` prints once you have verified it.
`--trust` doesn't help in CI: without a terminal it refuses, and it never accepts a key on its
own, not even with `--yes`. Pointing `sftp.known_hosts` at a committed, verified
`known_hosts` file also works.

**Rotating the server's key.** Before the switch, pin both fingerprints
(`host_key: ["SHA256:old...", "SHA256:new..."]`). After it, remove the old one. If you rely on
`known_hosts` instead, remove the host's old line from shipr's `known_hosts` (next to the global
config) or from `~/.ssh/known_hosts`. Then run `shipr test --trust` and compare the new
fingerprint before confirming.

## How it works

1. **Export.** `git archive` writes the chosen commit into a temp folder. Line endings are not
   converted, so Windows and Linux upload the same bytes.
2. **Build.** `build.commands` run there, and `build.output` is what gets deployed.
3. **Select.** Include and exclude rules (gitignore syntax) are applied, and each file is hashed
   with SHA-256.
4. **Compare.** For each file, shipr compares your local file, its manifest entry, and the
   server's current listing:

   | Situation | Action |
   |---|---|
   | unchanged | skip |
   | changed locally | upload |
   | edited on the server since the last deploy | back up, then overwrite (shown in the summary) |
   | deleted on the server | upload again |
   | already on the server but never deployed by shipr | upload only if the contents differ (backed up first) |
   | removed locally | ask, back up, then delete |
   | protected and present on the server | never touched |
   | on the server but not shipr's (uploads, logs, server-only config) | **never touched** |

5. **Upload.** Each file is written to a temporary name and then renamed into place. The manifest
   is checkpointed every 25 files, and a history record is written at the end.

Step-by-step diagrams of every stage, from config loading to the exit code, are in
[docs/FLOW.md](docs/FLOW.md).

shipr compares file contents rather than Git history. So a deploy that was interrupted or only
partly succeeded is completed by simply running it again, and a rollback is just a deploy of an
older commit.

## Safety

- **Server identity.** Host keys are always checked. Pin the key with `host_key` (best for CI),
  or accept an unknown host with `--trust`, which shows the fingerprint and asks you to confirm
  in a terminal.
- **Secrets.** They come only from environment variables or `.env`, are never echoed, and are
  never uploaded.
- **Path checks.** Every path is validated before anything changes. Nothing outside `remote_path`
  is touched, and shipr's own state directory cannot be overwritten.
- **Server symlinks.** shipr never writes, reads or deletes through a symlink, special file or
  unreadable directory on the server. Files at or under one are listed as `skip ... left
  untouched`. A symlink that appears mid-deploy fails that file instead of being followed.
- **Interrupted uploads.** Leftover temp and "parked" files are only removed when shipr can
  prove they are its own and recoverable. Otherwise the deploy stops and tells you what to check.
- **Deletions.** Only files shipr itself deployed can be deleted. You are always asked, and a
  "deleted" file is really moved into a backup folder. If `state_path` is on another filesystem,
  where the server can't move files into it, the file is copied there, the copy is verified by
  hash, and only then is the original removed.
- **Permissions.** A replaced file keeps the server's permissions. New files keep their executable
  bit, and backups keep the original mode. While a file is being uploaded, its temporary copy is
  readable only by the deploy user (0600) from the moment it is created.
- **`shipr.yml` is code.** Its commands run in a shell, the same way `package.json` scripts do.
  Review changes to it like code.

Details are in [docs/security.md](docs/security.md).

## Troubleshooting

| Message | What to do |
|---|---|
| `... is not a known host` | The error shows the server's fingerprint. Verify it with your host, then pin it as `host_key`, or run `shipr test -e ENV --trust` in a terminal and confirm. |
| `... was not confirmed, so it is not trusted` | `--trust` needs a terminal to ask you. In CI, pin `host_key` instead. |
| `HOST KEY MISMATCH` | The server key changed, or doesn't match your `host_key` pin. Confirm with your host why. Then update the pin, or remove the old line from `known_hosts` (see "Rotating the server's key"). |
| `authentication failed` | Check `username`, and the password or key. Check the `${VARIABLE}` is set (`shipr validate`). |
| `could not connect` | Check the host and port and your firewall. Raise `timeout` or `retries` for slow links. |
| `invalid YAML at line N ...: duplicate key` | The same key is given twice in one mapping; YAML would silently keep the last one. Remove one. |
| `invalid sftp settings` | The message names the setting: a typo (`hots`), a missing `host` or `username`, or a malformed `host_key`. |
| `build commands and local hooks see your whole environment` | Add `build.env` listing the variables your build needs (`env: []` if none). |
| `remote_path ... must be a normalised absolute directory` | Use a full path like `/home/u123/public_html`. `shipr test` prints your login directory. |
| `could not tell whether ... allows shell commands` | The shell check timed out or the connection dropped, so shipr can't tell whether your remote hooks would run. It stops before changing anything instead of skipping them. Deploy again. If the server really has no shell, set `ssh_exec: false`. |
| `server has no shell access; ... hooks skipped` | Expected on SFTP-only hosting. Set `ssh_exec: false` to skip the probe, or move the commands into `build.commands`. |
| `another deploy holds the lock` | Wait for it. If it is dead, run the `shipr unlock -e ENV --id ...` command the message prints. |
| `this deploy's lock was removed or taken over while it waited` | Someone ran `shipr unlock` while your deploy waited at a prompt. Nothing was changed. Check `shipr status`, then deploy again. |
| `the deploy lock is now held by another deploy` | This deploy's lock was removed while it ran, and another deploy took it. shipr left that lock alone. Check `shipr status` and `shipr history`: the two deploys may have overlapped. |
| `... matches N deployed commits` | `rollback --to` got a commit prefix that fits several deploys. Use more characters, or the deploy id from `shipr history`. |
| `could not confirm whether moving ... completed` | The connection dropped mid-delete, and shipr couldn't prove whether the file reached its backup folder. Nothing was repeated or undone. Check both paths it names, then deploy again. |
| `unknown git ref` / `commit ... is not in your local repo` | Run `git fetch`, then check the branch, tag or commit exists. |
| `you have uncommitted changes` | Only committed content is deployed. Commit first if you want those changes live. |
| Exit code `4` | Some files failed (shown as `FAILED path: reason`). Fix the cause (often permissions) and deploy again. |

## Known limitations

- **SFTP is path-based.** Every directory is re-checked with `lstat` right before each change,
  but someone who can write to the deploy folder could still swap in a symlink between that check
  and the request. The deploy user's permissions are the real boundary (see
  [docs/security.md](docs/security.md)).
- **The deploy lock is not an atomic compare-and-delete.** Releasing it is a read followed by a
  delete. Run `shipr unlock` only when no deploy is running. Locks never expire on their own.
- **A remote hook that times out may keep running on the server.** shipr stops waiting and fails
  the deploy; over SFTP it can't kill the remote process. Hooks may also run again on the next
  deploy, so make them safe to repeat.
- **Server-side edits are detected by size and modified time**, not content. An edit that keeps
  both unchanged goes unnoticed.
- **The default records folder is private only if `remote_path` is the web root.** For a site in
  a subfolder (e.g. `public_html/staging`), set `state_path` outside the web root.
- **Backups are never pruned.** Clean up `state_path/backups/` yourself.
- **Deletions with `state_path` on another filesystem** download and re-upload each file to verify
  the backup, which is slower. A file edited on the server between that check and its removal is
  backed up in its earlier version.
- **Servers that ignore the requested file mode** (some non-OpenSSH servers) leave a temp file at
  their default mode for one request, before any content is written.
- **shipr relies on paramiko internals** to create temp files 0600 (paramiko's public `open()`
  can't send a mode). paramiko is limited to `<6`, and a unit test fails if those internals
  change.
- **YAML merge lists with shared keys** (`<<: [*a, *b]` where both set the same key) resolve
  differently across PyYAML versions (6.0.4 changed it). Avoid overlapping keys in merge lists.
- **`uv pip install` checks only the lower Python bound**, so it installs shipr on 3.11.0–3.11.3,
  where shipr then refuses to run. pip, `uv tool` and `uv run` refuse up front.
- **Windows:** POSIX permission bits don't apply (files follow the folder's access list), and a
  timed-out local hook is stopped with `taskkill /T`. `known_hosts` appends are not guaranteed
  atomic; pin `host_key` instead.

## Upgrading from 0.1

shipr 0.2.0 is the first packaged release. "0.1" means the earlier development builds and their
legacy records layout (`<remote_path>/.shipr`); shipr 0.2.0 still reads that layout and leaves it
in place until you move it.

Read the "Breaking changes" in [CHANGELOG.md](CHANGELOG.md). The ones most likely to matter:

- Records now go beside `remote_path` by default. Existing records inside the web root keep being
  used, with a warning, until you move them (see "Where shipr keeps its records").
- `--trust` needs a terminal; CI must pin `sftp.host_key`.
- Hooks have a 30-minute default timeout and get no stdin.
- `shipr validate` rejects invalid SFTP settings and duplicate YAML keys.
- `shipr unlock --yes` alone no longer removes a lock; use `--id`.
- Python 3.10.12+, 3.11.4+ or 3.12+, and paramiko 4 or 5, are required.

## Development

```bash
git clone https://github.com/aayushdhimaann/shipr && cd shipr
uv sync                              # create .venv with all dev dependencies
uv run shipr --help                  # run the CLI from source

uv run ruff check src tests          # lint
uv run pytest                        # unit, end-to-end and CLI tests (fast, no network)
uv run pytest -m integration         # real SSH/SFTP servers in Docker (needs Docker)
```

Release checks, building and smoke-testing the installed CLI are described in
[docs/RELEASING.md](docs/RELEASING.md).

The integration suite starts two throwaway containers on random local ports and removes them
afterwards: `linuxserver/openssh-server` (full SSH) and `atmoz/sftp` (SFTP-only, like shared
hosting).

### Project layout

```
src/shipr/
  cli.py        argument parsing and output only
  engine.py     deploy + rollback orchestration (lock → plan → confirm → apply → record)
  admin.py      status, history, unlock, connection check
  workspace.py  git ref → exported commit → build → selected, hashed files
  planner.py    upload/delete decisions and path checks (pure, no I/O)
  executor.py   applies a plan; keeps the manifest and record in step per file
  hooks.py      local and remote commands, ssh_exec policy
  state.py      manifest, lock, history and backups on the server
  models.py     typed, validated records stored on the server
  config.py     shipr.yml schema, layering, ${VAR} interpolation
  files.py      selection rules and hashing
  gitops.py     git CLI wrapper
  scaffold.py   `shipr init` template
  ui.py         output/confirm callbacks and plan rendering
  errors.py     error hierarchy (each error carries its exit code)
  paths.py      per-user directories
  providers/    Provider interface + registry, sftp, local
tests/          unit/, end-to-end (local provider), CLI, integration/ (Docker)
examples/       shipr.yml for PHP, React, Node.js, Next.js, static sites
docs/           flow diagrams (FLOW.md), security model, provider guide, design notes (PLAN.md)
```

To add a deployment method (FTP, rsync, S3, ...), see [docs/providers.md](docs/providers.md).
Providers are separate packages that register through an entry point; the core does not change.

## Contributing, security, license

- Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first.
- Report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md).
- How every command works, as diagrams: [docs/FLOW.md](docs/FLOW.md).
- How a release is built and checked: [docs/RELEASING.md](docs/RELEASING.md).
- The design and roadmap are in [docs/PLAN.md](docs/PLAN.md).
- shipr is licensed under [MIT](LICENSE).
