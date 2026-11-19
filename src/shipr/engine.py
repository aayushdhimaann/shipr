"""Deploy orchestration: workspace (git + build) → lock → plan → confirm → execute → record.

Each step lives in its own module; this file only sequences them and owns the
success/failure bookkeeping.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass

from . import gitops, providers
from .config import Config, Environment
from .errors import Aborted, DeployError, ProviderError
from .executor import PlanExecutor
from .hooks import remote_exec_available, run_local, run_remote_phase
from .models import DeployRecord, InProgress, Kind, Manifest
from .planner import Op, Plan, make_plan, safe_join
from .providers import Provider, RemoteStat
from .state import RemoteState, new_deploy_id, now_iso, whoami
from .ui import UI, show_plan, show_result
from .workspace import Workspace, prepare

LEFTOVER_SUFFIXES = (".shipr-tmp", ".shipr-old")  # from uploads interrupted mid-swap


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
    return provider


def state_for(env: Environment, provider: Provider) -> RemoteState:
    return RemoteState(provider, env.effective_state_path,
                       inside_webroot=env.state_rel is not None)


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
        self.state = state_for(env, provider)
        self.record = DeployRecord(
            id=new_deploy_id(ws.sha), kind=options.kind, project=cfg.project_name,
            environment=env.name, commit=ws.sha, branch=ws.branch, subject=ws.subject,
            by=whoami(), target=provider.describe(), started=now_iso())
        self._stale_tmp: list[str] = []

    def run(self) -> DeployRecord:
        if self.options.dry_run:  # read-only: no lock, no writes
            self._load_and_plan()
            self.record.status = "dry-run"
            self.ui.echo("dry run: nothing was changed")
            return self.record

        self.state.acquire_lock(self.record.id)
        try:
            manifest, remote, plan = self._load_and_plan()
            if plan.empty and manifest.deployed_commit == self.ws.sha \
                    and not manifest.in_progress:
                self.record.status = "up-to-date"
                self.ui.echo("already up to date")
                return self.record
            deletes = self._confirm(plan)
            self._apply(manifest, remote, plan, deletes)
            show_result(self.ui, self.record)
            return self.record
        finally:
            try:
                self.state.release_lock(self.record.id)
            except Exception as e:  # never mask the deploy's own outcome
                self.ui.warn(f"could not release lock: {e} (run `shipr unlock`)")

    # steps -------------------------------------------------------------------
    def _load_and_plan(self) -> tuple[Manifest, dict[str, RemoteStat], Plan]:
        manifest = self.state.load_manifest()
        self._warn_about(manifest)
        remote = scan_remote(self.env, self.provider, self.state)
        self._stale_tmp = [r for r in remote if r.endswith(LEFTOVER_SUFFIXES)]
        for r in self._stale_tmp:
            del remote[r]
        plan = make_plan(self.ws.files, manifest.files, remote, self.ws.rules,
                         self._remote_hashes, reserved=self.env.state_rel)
        show_plan(self.ui, plan, self.ws.renames_since(manifest.deployed_commit))
        return manifest, remote, plan

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
        env, record = self.env, self.record
        has_remote_hooks = bool(env.hooks.remote_before or env.hooks.remote_after)
        can_exec = has_remote_hooks and remote_exec_available(env, self.provider)
        run_remote_phase(self.provider, env, env.hooks.remote_before, "remote_before", can_exec,
                         self.ui)

        self.state.ensure_private()
        manifest.in_progress = InProgress(deploy_id=record.id, by=record.by,
                                          started=record.started)
        self.state.save_manifest(manifest)
        t0 = time.monotonic()
        try:
            PlanExecutor(self.provider, self.state, manifest, record, self.ws.files, remote,
                         env.remote_path, self.ui).run(plan, deletes)
            self._remove_stale_tmp()
            record.status = "partial" if record.failed else "success"
            if record.failed and env.hooks.remote_after:
                self.ui.warn("some files failed; remote_after hooks skipped")
            else:
                run_remote_phase(self.provider, env, env.hooks.remote_after, "remote_after",
                                 can_exec, self.ui)
        except BaseException as e:
            record.status = "interrupted" if isinstance(e, KeyboardInterrupt) else "failed"
            record.error = str(e) or type(e).__name__
            raise
        finally:
            record.finished = now_iso()
            record.duration_s = round(time.monotonic() - t0, 2)
            self._save(manifest)

    def _remove_stale_tmp(self) -> None:
        for r in self._stale_tmp:
            try:
                self.provider.delete(safe_join(self.env.remote_path, r))
            except (OSError, ProviderError):
                pass  # harmless leftovers; retried next deploy

    def _save(self, manifest: Manifest) -> None:
        """Persist progress whatever happened; an interrupted run keeps `in_progress`."""
        record = self.record
        if record.completed:
            manifest.in_progress = None
            manifest.deployed_commit = record.commit
            manifest.branch = record.branch
            manifest.deploy_id = record.id
            manifest.deployed_at = record.finished
            manifest.environment = record.environment
        try:
            self.state.save_manifest(manifest)
            self.state.save_history(record)
        except Exception as e:  # don't hide the original error
            self.ui.warn(f"could not save deploy records: {e}")


def rollback(cfg: Config, env: Environment, to: str | None = None,
             options: DeployOptions | None = None, ui: UI | None = None) -> DeployRecord:
    """Redeploy an earlier commit; the normal diff then reverts exactly the changed files."""
    with open_provider(env) as provider:
        state = state_for(env, provider)
        current = state.load_manifest().deployed_commit
        records = [r for r in state.list_history() if r.completed]
    target = rollback_target(records, current, to)
    if not gitops.has_commit(target.commit, gitops.repo_root(cfg.dir)):
        raise DeployError(f"commit {target.commit[:7]} is not in your local repo; "
                          "run git fetch first")
    options = dataclasses.replace(options or DeployOptions(), kind="rollback")
    return deploy(cfg, env, target.commit, options, ui)


def rollback_target(records: list[DeployRecord], current: str | None,
                    to: str | None) -> DeployRecord:
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
