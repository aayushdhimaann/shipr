# How shipr works

Every step shipr takes for `deploy`, `rollback` and the other commands, from loading the config to the exit code. Section headers name the module and function that do the work, so you can jump straight to the code in `src/shipr/`.

Colours in the diagrams:
- blue: runs on your machine
- teal: talks to the server
- violet: writes shipr's records
- red: stops with an error

## Contents

- [Architecture](#architecture)
- [Deploy at a glance](#deploy-at-a-glance)
- [Stage 1: Load and validate the configuration](#stage-1-load-and-validate-the-configuration)
- [Stage 2: Prepare the workspace](#stage-2-prepare-the-workspace)
- [Stage 3: Connect to the server](#stage-3-connect-to-the-server)
- [Stage 4: Lock and plan](#stage-4-lock-and-plan)
- [Planner decisions](#planner-decisions)
- [Stage 5: Confirm and apply](#stage-5-confirm-and-apply)
- [One file upload](#one-file-upload)
- [Stage 6: Record, unlock and exit](#stage-6-record-unlock-and-exit)
- [Lock and recovery](#lock-and-recovery)
- [Rollback](#rollback)
- [Other commands](#other-commands)
- [What shipr keeps on the server](#what-shipr-keeps-on-the-server)
- [Errors and exit codes](#errors-and-exit-codes)

## Architecture

Dependencies point one way: the CLI calls the engine or admin commands, which use the workspace, planner, executor and state modules, which talk to a provider. Only the provider knows about SFTP.

```mermaid
flowchart TB
  user(["Developer or CI"]) --> cli
  subgraph LOCAL["Your machine"]
    cli["cli.py<br/>parse options · errors to exit codes"]
    cfg["config.py<br/>load, merge, validate shipr.yml"]
    eng["engine.py<br/>deploy · rollback"]
    adm["admin.py<br/>status · history · unlock · test"]
    ws["workspace.py<br/>commit → build → file list"]
    git["gitops.py<br/>git CLI"]
    fil["files.py<br/>rules + sha256"]
    hk["hooks.py<br/>local + remote commands"]
    pl["planner.py<br/>decisions, no I/O"]
    ex["executor.py<br/>applies the plan"]
    st["state.py<br/>manifest · lock · history · backups"]
    md["models.py<br/>typed records"]
    ui["ui.py<br/>echo · warn · confirm"]
    reg["providers registry<br/>entry points + built-ins"]
    base["providers/base.py<br/>Provider interface"]
    sftp["providers/sftp.py<br/>paramiko"]
    loc["providers/local.py<br/>folder target"]
  end
  subgraph SERVER["Server"]
    site["remote_path<br/>your site files"]
    rec[".shipr/<br/>manifest · lock · history · backups"]
  end
  cli --> cfg
  cli --> eng
  cli --> adm
  eng --> ws
  ws --> git
  ws --> fil
  ws --> hk
  eng --> pl
  eng --> ex
  eng --> st
  eng --> ui
  adm --> st
  ex --> st
  st --> md
  eng --> reg
  reg --> sftp
  reg --> loc
  sftp -. implements .-> base
  loc -. implements .-> base
  sftp == "SFTP + optional SSH shell" ==> site
  sftp ==> rec
  classDef local fill:#3e63dd1f,stroke:#3e63dd
  classDef srv fill:#0e8f7f24,stroke:#0e8f7f
  classDef recs fill:#7c4dce1f,stroke:#7c4dce
  class cli,cfg,eng,adm,ws,git,fil,hk,pl,ex,ui,md local
  class reg,base,sftp,loc,site srv
  class st,rec recs
```

- **Plugins.** A new method (FTP, rsync, S3) is a separate package registered under the `shipr.providers` entry point. The core doesn't change.
- **Pure planner.** `planner.py` never touches disk or network, so its decision table is unit-tested directly.
- **One error type.** Every expected failure is a `ShiprError` from `errors.py` that carries its own exit code.

## Deploy at a glance

The six stages of `shipr deploy`. Each one has a detailed diagram below.

```mermaid
flowchart LR
  A["1 · Load config<br/>shipr.yml + global + .env"] --> B["2 · Prepare workspace<br/>git archive → build → hash"]
  B --> C["3 · Connect<br/>SFTP + host key check"]
  C --> D["4 · Lock + plan<br/>files vs manifest vs server"]
  D --> E["5 · Confirm + apply<br/>hooks · backups · uploads · deletes"]
  E --> F["6 · Record + exit<br/>manifest · history · unlock"]
  classDef local fill:#3e63dd1f,stroke:#3e63dd
  classDef srv fill:#0e8f7f24,stroke:#0e8f7f
  classDef recs fill:#7c4dce1f,stroke:#7c4dce
  class A,B local
  class C,D,E srv
  class F recs
```

## Stage 1: Load and validate the configuration

Code: `cli.py · Ctx.env → config.load → Config.env`

```mermaid
flowchart TD
  s(["any command with -e ENV"]) --> f{"-c FILE given?"}
  f -- yes --> r
  f -- no --> up["search shipr.yml in the current folder,<br/>then each parent folder"]
  up -- "not found" --> e1["ConfigError: run shipr init"]
  up --> r["read YAML safely"]
  r --> g["read global defaults if present<br/>~/.config/shipr/config.yml"]
  g --> v{"version 1, only known top-level keys,<br/>at least one environment?"}
  v -- no --> e2["ConfigError"]
  v -- yes --> n{"-e given?"}
  n -- "no, several environments" --> e3["usage error · exit 2"]
  n -- "yes, or only one environment" --> m["deep-merge, later layers win<br/>1 global defaults<br/>2 project source, build, files, hooks<br/>3 project defaults<br/>4 environments.ENV<br/>(mappings merge, lists replace)"]
  m --> x["set aside hooks and build.commands<br/>(commands are never interpolated)"]
  x --> i["replace ${VAR} in every other value<br/>shell environment first, then .env (read literally)"]
  i -- "variable missing" --> e4["ConfigError names the variable"]
  i --> b["put the commands back"]
  b --> p["validate the Environment<br/>remote_path and state_path: normalised absolute, not /<br/>ssh_exec: auto, true or false<br/>error messages never echo values"]
  p -- invalid --> e5["ConfigError"]
  p --> k{"leftover keys are provider names,<br/>provider is registered?"}
  k -- no --> e6["ConfigError: unknown key or provider"]
  k -- yes --> ok(["Environment ready"])
  classDef err fill:#d23f4520,stroke:#d23f45
  classDef local fill:#3e63dd1f,stroke:#3e63dd
  class e1,e2,e3,e4,e5,e6 err
  class up,r,g,m,x,i,b,p local
```

- **Why commands stay literal.** An expanded secret would be printed when the command runs and stored in deploy history. Commands read `$VAR` from their environment instead.
- **Where the provider block comes from.** Any extra key that matches the provider name (e.g. `sftp:`) becomes `env.provider_settings`. The provider validates it with its own model.

## Stage 2: Prepare the workspace

Code: `workspace.prepare · resolve_target · gitops.export · hooks.run_local · files.collect`

All of this runs on your machine, in a temporary folder, before anything connects to the server. The temp folder is deleted when the deploy ends, whatever the outcome.

```mermaid
flowchart TD
  a(["Environment + optional --ref"]) --> r{"which commit?"}
  r -- "--ref given" --> rr["the --ref value"]
  r -- "else env.branch set" --> rb["env.branch"]
  r -- else --> rh["HEAD"]
  rr --> rp["git rev-parse --verify REF^{commit}"]
  rb --> rp
  rh --> rp
  rp -- fails --> e1["DeployError: unknown git ref"]
  rp --> w["print target line · warnings:<br/>uncommitted changes (only when deploying HEAD)<br/>submodules are not deployed"]
  w --> x["git -c core.autocrlf=false archive SHA<br/>extract the tar with the safe data filter<br/>(export-ignore honoured, no .git)"]
  x -- "absolute or escaping symlink" --> e2["GitError"]
  x --> pd["project_dir = export / folder of shipr.yml / source<br/>must exist inside the export"]
  pd -- missing --> e3["DeployError"]
  pd --> he["hook environment = your environment + .env values<br/>(or only build.env names + basics)<br/>+ SHIPR_ENV, SHIPR_COMMIT, SHIPR_PROJECT"]
  he --> lb["run hooks.local_before in project_dir"]
  lb --> bc["run build.commands one by one"]
  lb -- "non-zero exit" --> e4["DeployError: command failed"]
  bc -- "non-zero exit" --> e4
  bc --> od["out_dir = project_dir / build.output<br/>must exist inside the export"]
  od -- missing --> e3
  od --> c["collect files<br/>walk out_dir, prune excluded folders<br/>keep if include matches and exclude doesn't<br/>(gitignore syntax + built-in secret excludes)<br/>skip symlinks that point outside"]
  c --> h["per file: sha256, size, mode<br/>(exec bit kept, group/other write dropped)"]
  h --> done(["Workspace: commit, branch, subject, files, rules"])
  classDef err fill:#d23f4520,stroke:#d23f45
  classDef local fill:#3e63dd1f,stroke:#3e63dd
  class e1,e2,e3,e4 err
  class rp,w,x,pd,he,lb,bc,od,c,h local
```

- **Why git archive.** It deploys exactly what was committed. Line-ending conversion is turned off, so Windows and Linux upload identical bytes, and a killed deploy leaves nothing behind in your repo.
- **Built-in excludes.** `.git/ .shipr/ .env .env.* *.pem *.key id_rsa* id_ed25519* shipr.yml node_modules/ *.log` and shipr's own temp names.
- **Dry run still builds.** `local_before` and the build run in a dry run too, because the plan needs the real build output.

## Stage 3: Connect to the server

Code: `engine.open_provider · SFTPProvider.connect / _connect_once / _retry`

```mermaid
flowchart TD
  s(["open_provider(env)"]) --> reg["look up provider by name<br/>entry points shipr.providers + built-ins"]
  reg --> v["validate sftp settings<br/>host, port, username, password or key_file,<br/>passphrase, timeout, retries, known_hosts, host_key"]
  v --> ea["exec_allowed = ssh_exec is not false"]
  ea --> c["connection attempt"]
  c --> kh["load host keys<br/>~/.ssh/known_hosts + shipr known_hosts<br/>+ sftp.known_hosts file<br/>(none when host_key is pinned: the pin alone decides)"]
  kh --> pol{"unknown host: host_key pinned?"}
  pol -- "yes: matches" --> pin["accepted in memory<br/>(nothing written)"]
  pol -- "yes: differs" --> e4["ProviderError: HOST KEY MISMATCH"]
  pol -- no --> tr{"--trust and the user confirms<br/>the fingerprint in a terminal?"}
  tr -- no --> rej["rejected; the error shows the fingerprint<br/>and how to pin it"]
  tr -- yes --> tofu["saved to shipr known_hosts<br/>(one O_APPEND write)"]
  pin --> ssh["SSH handshake + login<br/>password · key + passphrase · ssh-agent<br/>then: a pinned key must match, even if known_hosts accepted it"]
  tofu --> ssh
  ssh -- "login refused" --> e1["ProviderError: authentication failed<br/>(no retry)"]
  ssh -- "server key changed" --> e2["ProviderError: HOST KEY MISMATCH<br/>(no retry)"]
  rej --> e3["ProviderError (no retry)"]
  ssh -- "network error or timeout" --> left{"attempts left?"}
  left -- yes --> wait["wait 1s, 2s, 4s … up to 10s"]
  wait --> c
  left -- no --> e4["ProviderError: could not connect"]
  ssh -- ok --> sf["open SFTP channel<br/>remember host fingerprint"]
  sf --> ready(["connected"])
  classDef err fill:#d23f4520,stroke:#d23f45
  classDef srv fill:#0e8f7f24,stroke:#0e8f7f
  class e1,e2,e3,e4 err
  class kh,ssh,sf,rej,tofu srv
```

### Every SFTP operation after connecting

```mermaid
flowchart LR
  op(["stat · walk · upload · download<br/>delete · rename · lock"]) --> run["run op with the current SFTP client"]
  run -- ok --> res(["result"])
  run -- error --> dead{"connection dead?<br/>SSH or EOF error, timeout,<br/>or socket closed"}
  dead -- "no: real error<br/>(permission, not found)" --> up["raised to the caller<br/>(executor records a failed file)"]
  dead -- yes --> rl{"retries left?"}
  rl -- yes --> rc["back off, reconnect"]
  rc --> run
  rl -- no --> cl["ConnectionLost<br/>deploy stops, progress is saved"]
  classDef err fill:#d23f4520,stroke:#d23f45
  classDef srv fill:#0e8f7f24,stroke:#0e8f7f
  class cl err
  class run,rc srv
```

- **Fresh client per attempt.** Each operation receives the live SFTP client as an argument, so a retry can never reuse a handle to a closed connection.

## Stage 4: Lock and plan

Code: `engine.Deployment.run · _load_and_plan · state.RemoteState`

```mermaid
flowchart TD
  s(["Deployment.run"]) --> dr{"--dry-run?"}
  dr -- yes --> lp1["load manifest + walk server + make plan<br/>(no lock, nothing written)"]
  lp1 --> show1["print plan · status dry-run · exit 0"]
  dr -- no --> lk["create .shipr/lock exclusively<br/>content: user@host, time, deploy id"]
  lk -- "already exists, other content" --> e1["LockedError: another deploy holds the lock<br/>exit 1"]
  lk -- "exists with our own content<br/>(reply lost in a reconnect)" --> man
  lk -- created --> man["read .shipr/manifest.json<br/>missing: empty manifest · corrupt: StateError"]
  man --> w1["warn if in_progress is set<br/>(earlier deploy was interrupted; this run finishes it)"]
  w1 --> w2["warn if the deployed commit is not an ancestor<br/>(rollback, branch switch or force-push)"]
  w2 --> walk["list every file under remote_path<br/>(skip the state folder)"]
  walk --> lo["set aside leftover .shipr-tmp and .shipr-old files<br/>for cleanup"]
  lo --> mk["make_plan<br/>(see Planner decisions)"]
  mk --> rn["rename hints: git diff -M deployed..target"]
  rn --> pr["print plan<br/>uploads with reasons · deletes · protected · totals"]
  pr --> nx(["Stage 5: confirm + apply"])
  classDef err fill:#d23f4520,stroke:#d23f45
  classDef srv fill:#0e8f7f24,stroke:#0e8f7f
  classDef recs fill:#7c4dce1f,stroke:#7c4dce
  class e1 err
  class walk,lp1 srv
  class lk,man recs
```

## Planner decisions

Code: `planner.make_plan · check_rel · stat_matches`

For every file, the planner compares three views: **L**, the file you are deploying (sha256 and size); **M**, its manifest entry from the last deploy (sha256, plus the size and modified time the server reported); and **R**, what the server lists right now.

```mermaid
flowchart TD
  s(["make_plan(L, M, R)"]) --> val["validate every path in L and M first<br/>relative only · no .. or . or // · no backslash or NUL<br/>not inside shipr's state folder"]
  val -- bad --> e["UnsafePath: stop before any change"]
  val --> loop["each local file"]
  val --> mloop["each manifest entry with no local file"]
  loop --> bl{"it or a folder above it is a symlink,<br/>special file or unreadable dir on the server?"}
  mloop --> bl2{"same check"}
  bl -- yes --> skip["skip · left untouched, reported"]
  bl2 -- yes --> skip
  bl -- no --> p{"protected and present on server?"}
  p -- yes --> prot["leave untouched"]
  p -- no --> inm{"in manifest?"}
  inm -- yes --> onr{"on server?"}
  onr -- no --> u1["upload · missing on server"]
  onr -- yes --> sm{"server size + mtime<br/>same as recorded?"}
  sm -- no --> u2["back up, then upload<br/>changed on server since last deploy"]
  sm -- yes --> hs{"local sha256<br/>same as recorded?"}
  hs -- no --> u3["upload · changed"]
  hs -- yes --> sk["skip · unchanged"]
  inm -- no --> onr2{"on server?"}
  onr2 -- no --> u4["upload · new"]
  onr2 -- yes --> sz{"same size?"}
  sz -- no --> u5["back up, then upload<br/>differs from server copy"]
  sz -- yes --> hash["hash the server copy<br/>sha256sum over SSH in batches of 200,<br/>else download and hash"]
  hash --> eq{"same content?"}
  eq -- yes --> ad["adopt · record in manifest, no upload"]
  eq -- no --> u5
  bl2 -- no --> gone{"still on server?"}
  gone -- no --> fg["forget the entry"]
  gone -- yes --> pr2{"protected?"}
  pr2 -- yes --> prot
  pr2 -- no --> del["delete candidate<br/>ask · back up · delete"]
  classDef err fill:#d23f4520,stroke:#d23f45
  classDef srv fill:#0e8f7f24,stroke:#0e8f7f
  class e err
  class hash srv
```

- **Server symlinks.** A file at or under a server symlink, special file or unreadable directory
  is never uploaded, deleted or forgotten, so shipr can't write through it into another folder.
  Providers re-check every directory with `lstat` right before each change, which catches a
  symlink that appears after planning. That file then fails and the deploy ends `partial`.
- **Leftovers.** Temp and parked files from an interrupted upload of a managed file are taken out
  of R. A park is removed after the run only if it matches the deployed version or a backup.
  Otherwise the deploy stops before any change. Look-alikes of other files are ordinary server
  files.
- **Files shipr never deployed.** A file that is on the server but in neither L nor M (user uploads, logs, server-only config) is never planned, so it can't be overwritten or deleted.
- **Why size + mtime for the server.** Reading them costs one directory listing per folder; hashing would mean downloading every file. The limit: an edit that keeps both the size and the old mtime goes unnoticed.

## Stage 5: Confirm and apply

Code: `Deployment._confirm · _apply · executor.PlanExecutor · hooks.run_remote_phase`

```mermaid
flowchart TD
  s(["plan printed, lock held"]) --> utd{"nothing to do, same commit,<br/>no interrupted deploy?"}
  utd -- yes --> u["already up to date · exit 0"]
  utd -- no --> pc{"env.confirm and no --yes?"}
  pc -- yes --> a1["ask: Deploy SHA to ENV?"]
  a1 -- no --> ab["Aborted · exit 3 · nothing changed"]
  a1 -- yes --> dl
  pc -- no --> dl{"deletions planned?"}
  dl -- no --> hk
  dl -- yes --> nd{"--no-delete?"}
  nd -- yes --> keep["keep them on the server<br/>recorded as kept, offered again next deploy"]
  nd -- no --> yy{"--yes?"}
  yy -- yes --> apr["approve all deletions"]
  yy -- no --> tty{"running in a terminal?"}
  tty -- no --> keep
  tty -- yes --> a2["ask, showing the full list"]
  a2 -- no --> keep
  a2 -- yes --> apr
  keep --> hk{"remote hooks configured?"}
  apr --> hk
  hk -- no --> pv
  hk -- yes --> pb["probe shell: echo shipr-ok (with timeout)<br/>ssh_exec false: no · true: required or DeployError"]
  pb --> rb["remote_before: cd remote_path, run each command<br/>skipped with a warning if no shell"]
  rb -- fails --> e1["DeployError · status failed"]
  rb --> pv["write .shipr/.htaccess deny<br/>(state folder inside the web root)"]
  pv --> ip["manifest.in_progress = id, user, start · save"]
  ip --> ex1["executor step 1: adopt identical files,<br/>forget vanished entries"]
  ex1 --> ex2["executor step 2: each upload<br/>(see One file upload)"]
  ex2 --> ex3["executor step 3: each approved deletion<br/>move into backups/ID/path · drop from manifest<br/>remove folders left empty, up to remote_path"]
  ex3 --> cln["remove leftover temp files"]
  ex2 -. "every 25 operations" .-> ck["checkpoint manifest.json"]
  ex3 -. "every 25 operations" .-> ck
  ex2 -- "ConnectionLost" --> e2["stop · status failed"]
  cln --> anyf{"any file failed?"}
  anyf -- yes --> part["status partial<br/>remote_after skipped"]
  anyf -- no --> succ["status success"]
  succ --> ra["remote_after on the server<br/>skipped with a warning if no shell"]
  ra -- fails --> e1
  classDef err fill:#d23f4520,stroke:#d23f45
  classDef srv fill:#0e8f7f24,stroke:#0e8f7f
  classDef recs fill:#7c4dce1f,stroke:#7c4dce
  class ab,e1,e2 err
  class pb,rb,ex1,ex2,ex3,cln,ra srv
  class pv,ip,ck recs
```

- **Per-file failures.** Permission denied or a full disk on one file is recorded under `failed` and the deploy carries on. Only a lost connection stops it.
- **"Delete" is a move.** Deleted files are moved into `.shipr/backups/<deploy-id>/`, so every deletion can be undone by hand.

## One file upload

Code: `PlanExecutor._upload · Provider.copy · SFTPProvider.upload / _replace`

```mermaid
sequenceDiagram
  autonumber
  participant X as executor.py
  participant P as SFTP provider
  participant S as Server
  X->>X: target = safe_join(remote_path, path)
  alt marked for backup and the file exists on the server
    X->>P: copy(target, backups/ID/path)
    P->>S: stat target (keep its mode)
    P->>S: download target to a local temp file
    P->>S: upload temp into backups/ID/path with the same mode
  end
  X->>P: upload(local file, target, mode)
  Note over X,P: mode = server file's mode if it exists, else local mode
  P->>S: create missing parent folders
  P->>S: put file as .name.shipr-tmp (size confirmed)
  P->>S: chmod the temp file
  alt server supports posix-rename
    P->>S: posix_rename temp over target (atomic)
  else fallback
    P->>S: check the temp file still exists
    P->>S: rename target to target.shipr-old
    P->>S: rename temp to target
    P->>S: remove target.shipr-old
    Note over P,S: if the swap fails, target.shipr-old is renamed back
  end
  P->>S: stat target
  P-->>X: size, mtime, mode as the server reports them
  X->>X: manifest.files[path] = sha256, size, mtime
  X->>X: record.uploaded += path, reason, backup
```

- **Why the temp name.** Visitors never see a half-written file: the real name only ever points to a complete file.
- **Why record the server's stat.** Next deploy, a different size or mtime on the server means someone edited the file after shipr did, so it gets backed up first.

## Stage 6: Record, unlock and exit

Code: `Deployment._apply finally · _save · run finally · engine.deploy · cli.ShiprGroup`

```mermaid
flowchart TD
  ok(["status success or partial"]) --> fin
  bad(["exception: error → failed<br/>Ctrl-C or SIGTERM → interrupted"]) --> fin
  fin["always: set finished time and duration"] --> c{"files_done?<br/>(every file operation attempted,<br/>even if a hook failed after)"}
  c -- yes --> m["manifest: clear in_progress<br/>set deployed_commit, branch, deploy_id, deployed_at<br/>hooks_pending if remote_after failed or didn't run"]
  c -- no --> keepip["manifest keeps in_progress<br/>(progress so far is saved)"]
  m --> sv["save manifest.json, then history/ID.json<br/>(record notes files_done, hooks, manifest_saved)"]
  keepip --> sv
  sv --> se{"both saved?"}
  se -- no --> serr["StateError after the result: exit 1, never 0<br/>(a warning only, when another error is already being reported)"]
  se -- yes --> res
  serr --> res
  res["print result: counts, duration, stages, FAILED lines"]
  res --> rel["release the lock, only if it still names this deploy id"]
  rel --> cls["close the connection · delete the temp export"]
  cls --> la{"success and something changed?"}
  la -- yes --> lah["run hooks.local_after on your machine"]
  la -- no --> code
  lah --> code["exit code<br/>0 success · 4 partial · 1 failed · 130 interrupted"]
  classDef recs fill:#7c4dce1f,stroke:#7c4dce
  classDef srv fill:#0e8f7f24,stroke:#0e8f7f
  class m,keepip,sv recs
  class rel srv
```

## Lock and recovery

Code: `state.acquire_lock / release_lock · manifest.in_progress · cli SIGTERM handler`

How a deploy moves through the lock and the `in_progress` marker, and how an interrupted run is finished.

```mermaid
stateDiagram-v2
  [*] --> Unlocked
  Unlocked --> Locked: deploy creates .shipr/lock
  Locked --> LockBusy: lock already held
  LockBusy --> Unlocked: wait, or shipr unlock if the holder is dead
  Locked --> Unlocked: up to date, or cancelled at a prompt
  Locked --> InProgress: in_progress saved to manifest
  InProgress --> InProgress: checkpoint every 25 files
  InProgress --> Recorded: files done (hooks may have failed)
  InProgress --> Interrupted: Ctrl-C, kill, lost connection, error
  Recorded --> Unlocked: own lock released
  Interrupted --> Unlocked: own lock released, in_progress kept
  InProgress --> Stale: kill -9 or power loss
  Stale --> LockBusy: next deploy finds the old lock
  Unlocked --> Resuming: next deploy sees in_progress
  Resuming --> InProgress: re-plan uploads only what is still missing
```

- **SIGTERM is Ctrl-C.** The CLI turns `kill` into the same interrupt as Ctrl-C, so progress is saved and the lock released.
- **Resuming needs no special code.** The next run compares contents again. Anything already uploaded is skipped or adopted, and the rest is uploaded.

## Rollback

Code: `engine.rollback · rollback_target`

```mermaid
flowchart TD
  s(["shipr rollback -e ENV, optional --to ID or COMMIT"]) --> c["connect · read manifest.deployed_commit<br/>and history (deploys whose files finished)"]
  c --> ag{"no --to, and the last such deploy<br/>differs from the manifest's commit?"}
  ag -- yes --> e0["DeployError: history and manifest disagree,<br/>choose with --to"]
  ag -- no --> ip{"no --to and the last deploy<br/>was interrupted?"}
  ip -- yes --> lc["the last completed deploy<br/>(undo the half-finished one)"]
  lc --> h
  ip -- no --> t{"--to given?"}
  t -- yes --> m["latest deploy whose id matches<br/>or whose commit starts with the value"]
  m -- none --> e1["DeployError: no matching deploy"]
  t -- no --> b["find the deploy that introduced the current commit<br/>(ignoring later rollbacks to it), then take the<br/>latest earlier deploy with a different commit"]
  b -- none --> e2["DeployError: nothing earlier"]
  m --> h{"commit exists in your local repo?"}
  b --> h
  h -- no --> e3["DeployError: run git fetch first"]
  h -- yes --> d["normal deploy of that commit, kind rollback<br/>stages 2 to 6 run unchanged:<br/>only differing files are re-uploaded,<br/>files added since are offered for deletion"]
  classDef err fill:#d23f4520,stroke:#d23f45
  classDef srv fill:#0e8f7f24,stroke:#0e8f7f
  class e0,e1,e2,e3 err
  class c srv
```

- **Repeated rollbacks.** History A → B → C, then rollback to B. Rolling back again goes to A, not back to C.

## Other commands

Code: `admin.status · admin.history · admin.unlock · admin.check_connection · cli init / validate`

```mermaid
flowchart TB
  subgraph S1["shipr status"]
    direction LR
    s1["connect"] --> s2["read manifest and lock"] --> s3["list remote_path"] --> s4["drift: tracked files missing,<br/>or size/mtime changed"] --> s5["compare env branch head<br/>with the deployed commit"]
  end
  subgraph S2["shipr history"]
    direction LR
    h1["connect"] --> h2["read history/*.json oldest first<br/>(unreadable records skipped)"] --> h3["list the last N,<br/>or --show ID as JSON"]
  end
  subgraph S3["shipr unlock"]
    direction LR
    u1["confirm, or -y"] --> u2["connect"] --> u3["read and delete .shipr/lock"]
  end
  subgraph S4["shipr test"]
    direction LR
    t1["connect, --trust allowed"] --> t2["host key fingerprint,<br/>login folder"] --> t3["create and delete a probe file<br/>in the state folder"] --> t4["count files in remote_path"] --> t5["shell probe"] --> t6["note if records sit in the web root"]
  end
  subgraph S5["shipr init and validate (no connection)"]
    direction LR
    i1["init: look at package.json or composer.json"] --> i2["write commented shipr.yml"]
    v1["validate: Config.env for every environment"] --> v2["print provider and remote_path, or the error"]
  end
```

## What shipr keeps on the server

Code: `state.RemoteState · models.Manifest · models.DeployRecord`

Everything lives under `state_path`, by default beside `remote_path`: `<parent of remote_path>/.shipr/<name of remote_path>`, so `/var/www/html` → `/var/www/.shipr/html`. That is outside the site only when `remote_path` is the web root. If the default has no `manifest.json` but `<remote_path>/.shipr/manifest.json` exists (shipr 0.1's default), the old location is used and a warning explains how to move it. If both exist, shipr stops and asks. Because it is on the server, every teammate and every CI run sees the same state. `manifest.json` also records the `project` and `remote_path` it belongs to. A deploy, `status` or `rollback` whose `remote_path` doesn't match refuses to use it.

```text
.shipr/
├── manifest.json        # files: sha256, size, mtime · deployed_commit · in_progress · hooks_pending · project, remote_path
├── lock                 # present while a deploy runs: user@host, time, deploy id
├── .htaccess            # deny web access, Apache only (only when inside the web root)
├── history/
│   └── 20261119T103205123Z-3f2a9c1.json   # one record per deploy: who, when, commit, every file, status
└── backups/
    └── 20261119T103205123Z-3f2a9c1/       # server copies saved before overwrite or delete, same paths
```

## Errors and exit codes

Code: `errors.py · cli.ShiprGroup.invoke`

| Code | When | Raised by |
|---|---|---|
| 0 | Success, already up to date, or a dry run | — |
| 1 | Config, git, connection, lock, state or command failure | `ConfigError`, `GitError`, `ProviderError`, `LockedError`, `StateError`, `DeployError`, `UnsafePath` |
| 2 | Wrong command-line usage, e.g. several environments and no `-e` | click |
| 3 | You answered no at a confirmation | `Aborted` |
| 4 | Partial deploy: some files failed. Deploy again to retry them. | record status `partial` |
| 130 | Ctrl-C or SIGTERM. Progress was saved. | `KeyboardInterrupt` |
