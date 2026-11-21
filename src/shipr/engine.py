"""Deploy orchestration: workspace (git + build) → lock → plan → confirm → execute → record.

Each step lives in its own module; this file only sequences them and owns the
success/failure bookkeeping.
"""

from __future__ import annotations

import dataclasses
import posixpath
import time
from dataclasses import dataclass

from . import gitops, providers
from .config import Config, Environment
from .errors import Aborted, DeployError, ProviderError, StateError, UnsafePath
from .executor import PlanExecutor
from .hooks import remote_exec_available, run_local, run_remote_phase
from .models import DeployRecord, InProgress, Kind, Manifest
from .planner import Op, Plan, make_plan, safe_join
from .providers import Provider, RemoteStat
from .providers.base import leftover_of
from .state import RemoteState, new_deploy_id, now_iso, whoami
from .ui import UI, show_plan, show_result
from .workspace import Workspace, prepare


@dataclass(frozen=True)
class DeployOptions:
    dry_run: bool = False
    yes: bool = False  # confirm the deploy and deletions without asking
    no_delete: bool = False
    trust_new_host: bool = False
    kind: Kind = "deploy"


def open_provider(env: Environment, trust_new_host: bool = False) -> Provider:
    provider = providers.get(env.provider)(env.provider_settings, trust_new_host=trust_new_host)
    provider.exec_allowed = env.ssh_exec is not False
    provider.roots = (env.remote_path, env.effective_state_path)
    return provider


def state_for(env: Environment, provider: Provider) -> RemoteState:
    """The environment's records: `state_path`, else the default beside the web root. Records
    already at shipr 0.1's default (inside the web root) keep being used until moved."""
    root, legacy = env.effective_state_path, False
    if env.state_path is None \
            and provider.stat(posixpath.join(root, "manifest.json")) is None \
            and provider.stat(posixpath.join(env.legacy_state_path, "manifest.json")):
        root, legacy = env.legacy_state_path, True
    return RemoteState(provider, root, env.remote_path, legacy)


def state_warnings(env: Environment, state: RemoteState) -> list[str]:
    if state.legacy:
        return [f"deploy records are in {state.root}, inside the web root (shipr 0.1's "
                "default): anyone who can reach it over HTTP can read the manifest, history "
                "and backups, and backed-up scripts may still run. Move that folder to "
                f"{env.effective_state_path} (shipr then finds it there), or set "
                f"`state_path: {state.root}` to keep it where it is."]
    if state.inside_webroot:
        return [f"state_path {state.root} is inside the web root: deploy records and backups "
                "(including old scripts) are reachable over HTTP unless your web server denies "
                "that path. .htaccess only helps on Apache with AllowOverride; see "
                "docs/security.md."]
    return []


def scan_remote(env: Environment, provider: Provider, state: RemoteState) -> dict[str, RemoteStat]:
    """Files currently under remote_path, excluding shipr's own state dir."""
    return provider.walk(env.remote_path, skip=state.root if state.inside_webroot else None)


def deploy(cfg: Config, env: Environment, ref: str | None = None,
           options: DeployOptions | None = None, ui: UI | None = None) -> DeployRecord:
    options = options or DeployOptions()
    ui = ui or UI()
    with prepare(cfg, env, ref, ui) as ws:
        with open_provider(env, options.trust_new_host) as provider:
            record = Deployment(cfg, env, provider, ws, options, ui).run()
        if record.status == "success" and record.changed:
            run_local(env.hooks.local_after, ws.project_dir, ws.hook_env, ui)
    return record


class Deployment:
    """One run against one environment. Holds the remote lock for the whole run."""

    def __init__(self, cfg: Config, env: Environment, provider: Provider, ws: Workspace,
                 options: DeployOptions, ui: UI):
        self.env, self.provider, self.ws, self.options, self.ui = env, provider, ws, options, ui
        self.project = cfg.project_name
        self.state = state_for(env, provider)
        self._save_error: str | None = None
        self.record = DeployRecord(
            id=new_deploy_id(ws.sha), kind=options.kind, project=cfg.project_name,
            environment=env.name, commit=ws.sha, branch=ws.branch, subject=ws.subject,
            by=whoami(), target=provider.describe(), started=now_iso())
        self._leftovers: list[str] = []  # shipr's own leftovers, safe to remove after the run

    def run(self) -> DeployRecord:
        for w in state_warnings(self.env, self.state):
            self.ui.warn(w)
        if self.options.dry_run:  # read-only: no lock, no writes
            self._load_and_plan()
            self.record.status = "dry-run"
            self.ui.echo("dry run: nothing was changed")
            return self.record

        self._lock()
        try:
            manifest, remote, plan = self._load_and_plan()
            if plan.empty and manifest.deployed_commit == self.ws.sha \
                    and not manifest.in_progress and not manifest.hooks_pending:
                self.record.status = "up-to-date"
                self.ui.echo("already up to date")
                return self.record
            deletes = self._confirm(plan)
            self._apply(manifest, remote, plan, deletes)
            show_result(self.ui, self.record)
            if self._save_error:
                raise StateError(self._save_error)
            return self.record
        finally:
            try:
                self.state.release_lock(self.record.id)
            except Exception as e:  # never mask the deploy's own outcome
                self.ui.warn(f"could not release lock: {e} (run `shipr unlock`)")

    # steps -------------------------------------------------------------------
    def _lock(self) -> None:
        """Take the deploy lock; creating it also creates the state dir, so a state dir the
        deploy user can't create is reported here, before anything changes."""
        try:
            self.state.acquire_lock(self.record.id)
        except (OSError, ProviderError) as e:
            env = self.env
            fix = (f"By default shipr keeps them beside the web root. Set `state_path` in "
                   "shipr.yml to a directory the deploy user can write outside the web root "
                   f"(e.g. /home/<user>/.shipr/{posixpath.basename(env.remote_path)}), or to "
                   f"{env.legacy_state_path} to keep them inside the web root (shipr warns on "
                   "every deploy)." if env.state_path is None
                   else "Check that the deploy user can create and write it.")
            raise StateError(f"cannot create shipr's deploy records in {self.state.root} "
                             f"({e}). {fix}") from None

    def _load_and_plan(self) -> tuple[Manifest, dict[str, RemoteStat], Plan]:
        manifest = self.state.load_manifest()
        for w in self.state.check_owner(manifest, self.project):
            self.ui.warn(w)
        self._warn_about(manifest)
        remote = scan_remote(self.env, self.provider, self.state)
        self._leftovers = self._own_leftovers(manifest, remote)
        plan = make_plan(self.ws.files, manifest.files, remote, self.ws.rules,
                         self._remote_hashes, reserved=self.state.rel,
                         recovering=manifest.in_progress is not None)
        show_plan(self.ui, plan, self.ws.renames_since(manifest.deployed_commit))
        if plan.blocked:
            self.record.blocked = [rel for rel, _ in plan.blocked]
            self.ui.warn(f"{len(plan.blocked)} paths left untouched: the server has a symlink, "
                         "special file or unreadable directory there (see `skip` above)")
        return manifest, remote, plan

    def _own_leftovers(self, manifest: Manifest, remote: dict[str, RemoteStat]) -> list[str]:
        """Temp and park files from interrupted uploads of files shipr manages. They are taken
        out of `remote` (not site files) and returned for removal after the run. A park holds
        the server's previous copy of a file: unless it provably matches the deployed version
        or a backup, it may be the only copy, so stop before changing anything. Look-alikes
        of files shipr doesn't manage are ordinary server files and stay untouched."""
        managed = set(manifest.files) | set(self.ws.files)
        tmps, parks = [], []
        for rel, r in remote.items():
            own = leftover_of(rel)
            if own and r.kind == "file" and own[1] in managed:
                (tmps if own[0] == "tmp" else parks).append((rel, own[1]))
        for rel, _ in tmps + parks:
            del remote[rel]
        unproven = self._unproven_parks(parks, manifest)
        if unproven:
            raise DeployError(
                "refusing to deploy: shipr cannot prove these files are recoverable. Each holds "
                "the previous server copy of a file, parked by an upload that was interrupted, "
                "and matches neither the deployed version nor a backup, so it may be the only "
                "copy of changes made on the server:\n"
                + "\n".join(f"  {safe_join(self.env.remote_path, p)}  (previous copy of {b})"
                            for p, b in unproven)
                + "\nInspect each one, move it out of the deploy folder (or delete it if it "
                "isn't needed), then deploy again.")
        return [rel for rel, _ in tmps + parks]

    def _unproven_parks(self, parks: list[tuple[str, str]],
                        manifest: Manifest) -> list[tuple[str, str]]:
        if not parks:
            return []
        copies = self.state.backup_copies({base for _, base in parks})
        abs_park = {rel: safe_join(self.env.remote_path, rel) for rel, _ in parks}
        paths = list(abs_park.values()) + [p for ps in copies.values() for p in ps]
        # hash by downloading, not via sha256sum output (C-16: its parsing is not trusted yet)
        digest = Provider.sha256(self.provider, paths)
        unproven = []
        for rel, base in parks:
            h = digest.get(abs_park[rel])
            entry = manifest.files.get(base)
            known = {digest.get(c) for c in copies.get(base, [])}
            if entry:
                known.add(entry.sha256)
            if not h or h not in known:
                unproven.append((rel, base))
        return unproven

    def _warn_about(self, manifest: Manifest) -> None:
        if manifest.in_progress:
            ip = manifest.in_progress
            self.ui.warn(f"deploy {ip.deploy_id} by {ip.by} was interrupted; "
                         "this run will finish the job")
        prev = manifest.deployed_commit
        if prev and prev != self.ws.sha and not self.ws.contains(prev):
            self.ui.warn(f"{self.ws.sha[:7]} does not contain the deployed commit "
                         f"{prev[:7]} (rollback, branch switch or force-push)")

    def _remote_hashes(self, rels: list[str]) -> dict[str, str]:
        abs_of = {r: safe_join(self.env.remote_path, r) for r in rels}
        by_abs = self.provider.sha256(list(abs_of.values()))
        return {r: by_abs.get(a, "") for r, a in abs_of.items()}

    def _confirm(self, plan: Plan) -> list[Op]:
        """Ask what needs asking; return the deletions that were approved."""
        env, opts, ui = self.env, self.options, self.ui
        if env.confirm and not opts.yes and not ui.confirm(
                f"Deploy {self.ws.sha[:7]} to {env.name.upper()} "
                f"({self.provider.describe()}{env.remote_path})?"):
            raise Aborted("deploy cancelled")
        if not plan.deletes:
            return []
        approved = not opts.no_delete and (opts.yes or ui.confirm(
            f"Delete these {len(plan.deletes)} files from the server (they are backed up "
            "first)?\n" + "\n".join(f"  {o.path}" for o in plan.deletes) + "\n"))
        if approved:
            return plan.deletes
        self.record.kept = [o.path for o in plan.deletes]  # asked again next deploy
        ui.echo(f"keeping {len(self.record.kept)} removed files on the server")
        return []

    def _apply(self, manifest: Manifest, remote: dict[str, RemoteStat], plan: Plan,
               deletes: list[Op]) -> None:
        """Hooks, file operations and records, each stage noted on the record. Everything
        from the first remote hook on is recorded in history, whatever happens."""
        env, record = self.env, self.record
        manifest.project, manifest.remote_path = self.project, env.remote_path
        manifest.environment = env.name
        has_remote_hooks = bool(env.hooks.remote_before or env.hooks.remote_after)
        t0 = time.monotonic()
        failed = True
        try:
            can_exec = has_remote_hooks and remote_exec_available(env, self.provider)
            if has_remote_hooks:
                record.hooks = "not-run" if can_exec else "skipped"
            self._hooks(env.hooks.remote_before, "remote_before", can_exec)

            manifest.in_progress = InProgress(deploy_id=record.id, by=record.by,
                                              started=record.started)
            try:
                self.state.ensure_private()
                self.state.save_manifest(manifest)  # before any file changes
            except (OSError, ProviderError) as e:
                raise StateError(f"cannot write deploy records to {self.state.root} ({e}); "
                                 "nothing on the server was changed") from None
            PlanExecutor(self.provider, self.state, manifest, record, self.ws.files, remote,
                         env.remote_path, self.ui).run(plan, deletes)
            record.files_done = True
            self._remove_leftovers()
            record.status = "partial" if record.failed else "success"
            if record.failed and env.hooks.remote_after:
                self.ui.warn("some files failed; remote_after hooks skipped (they run on the "
                             "next deploy)")
            else:
                self._hooks(env.hooks.remote_after, "remote_after", can_exec)
                if can_exec:
                    record.hooks = "ok"
            failed = False
        except BaseException as e:
            record.status = "interrupted" if isinstance(e, KeyboardInterrupt) else "failed"
            record.error = str(e) or type(e).__name__
            raise
        finally:
            record.finished = now_iso()
            record.duration_s = round(time.monotonic() - t0, 2)
            self._save_error = self._save(manifest)
            if self._save_error and failed:  # don't hide the original error
                self.ui.warn(self._save_error)

    def _hooks(self, commands: list[str], phase: str, can_exec: bool) -> None:
        try:
            run_remote_phase(self.provider, self.env, commands, phase, can_exec, self.ui)
        except BaseException:
            self.record.hooks = "failed"
            raise

    def _remove_leftovers(self) -> None:
        for rel in self._leftovers:
            try:
                self.provider.delete(safe_join(self.env.remote_path, rel))
                self.record.cleaned.append(rel)
            except (OSError, ProviderError, UnsafePath):
                pass  # verified leftovers; retried next deploy

    def _save(self, manifest: Manifest) -> str | None:
        """Persist the manifest, then the history record, whatever happened. The manifest
        names the deployed commit as soon as its files are on the server, even if a hook then
        failed; an interrupted run keeps `in_progress`. Returns a message if anything could
        not be saved: such a deploy never exits 0."""
        record = self.record
        if record.files_done:
            manifest.in_progress = None
            manifest.deployed_commit = record.commit
            manifest.branch = record.branch
            manifest.deploy_id = record.id
            manifest.deployed_at = record.finished
            manifest.hooks_pending = record.hooks in ("not-run", "failed")
        problems = []
        try:
            self.state.save_manifest(manifest)
            record.manifest_saved = True
        except Exception as e:
            problems.append(f"manifest: {e}")
            if record.status in ("success", "partial"):
                record.status = "failed"
                record.error = f"the manifest could not be saved: {e}"
        try:
            self.state.save_history(record)
        except Exception as e:
            problems.append(f"history: {e}")
        if not problems:
            return None
        return (f"deploy records could not be saved ({'; '.join(problems)}). The server may "
                "not match shipr's records: check `shipr status`, then deploy again once "
                f"{self.state.root} accepts writes.")


def rollback(cfg: Config, env: Environment, to: str | None = None,
             options: DeployOptions | None = None, ui: UI | None = None) -> DeployRecord:
    """Redeploy an earlier commit; the normal diff then reverts exactly the changed files.
    The target comes from history (deploys whose files reached the server), cross-checked
    against the manifest; when they disagree, the user picks explicitly."""
    ui = ui or UI()
    with open_provider(env) as provider:
        state = state_for(env, provider)
        manifest = state.load_manifest()
        for w in state.check_owner(manifest, cfg.project_name):
            ui.warn(w)
        records = [r for r in state.list_history() if r.completed]
    current = manifest.deployed_commit
    if not to and records and current and records[-1].commit != current:
        last = records[-1]
        raise DeployError(
            "shipr's history and manifest disagree about what is deployed: the last deploy "
            f"that reached the server ({last.id}) put {last.commit[:7]} there, but the manifest "
            f"records {current[:7]}. Choose the version to restore with --to <deploy id> "
            "(see `shipr history`).")
    interrupted = manifest.in_progress is not None
    target = rollback_target(records, current, to, interrupted=interrupted)
    ui.echo(f"rolling back {env.name} from {(current or '?')[:7]} to {target.commit[:7]} "
            f"(deploy {target.id})"
            + ("; the last deploy was interrupted, so this restores the last one that "
               "completed" if interrupted and not to else ""))
    if not gitops.has_commit(target.commit, gitops.repo_root(cfg.dir)):
        raise DeployError(f"commit {target.commit[:7]} is not in your local repo; "
                          "run git fetch first")
    options = dataclasses.replace(options or DeployOptions(), kind="rollback")
    return deploy(cfg, env, target.commit, options, ui)


def rollback_target(records: list[DeployRecord], current: str | None,
                    to: str | None, interrupted: bool = False) -> DeployRecord:
    if interrupted and not to:
        # the server holds a mix: undo the interrupted deploy, back to the last completed one
        same = [r for r in records if r.commit == current]
        if not same:
            raise DeployError("the last deploy was interrupted and no completed deploy is "
                              "recorded; choose one with --to (see `shipr history`)")
        return same[-1]
    if to:
        matches = [r for r in records if r.id == to or r.commit.startswith(to)]
        if not matches:
            raise DeployError(f"no successful deploy matches {to!r} (see `shipr history`)")
        return matches[-1]
    # Step back past the deploy that *introduced* the current commit, not a later rollback
    # to it, so repeated rollbacks keep going back instead of toggling.
    idx = max((i for i, r in enumerate(records)
               if r.commit == current and r.kind != "rollback"), default=len(records))
    older = [r for r in records[:idx] if r.commit != current]
    if not older:
        raise DeployError("no earlier deployed commit to roll back to")
    return older[-1]
