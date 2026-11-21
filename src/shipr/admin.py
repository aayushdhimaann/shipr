"""Read-only and maintenance operations: status, history, unlock, connection check."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field

from .config import Environment
from .engine import (
    default_location_warning,
    open_provider,
    scan_remote,
    state_for,
    state_warnings,
)
from .errors import Aborted, DeployError, LockedError, ProviderError
from .hooks import remote_exec_available
from .models import DeployRecord, Manifest
from .planner import blocked_reason, stat_matches
from .state import LockInfo
from .ui import UI


@dataclass
class Status:
    manifest: Manifest
    drift: list[tuple[str, str]]  # (path, why)
    lock: str | None
    target: str
    state_root: str = ""
    warnings: list[str] = field(default_factory=list)


def status(env: Environment, project: str) -> Status:
    with open_provider(env) as provider:
        state = state_for(env, provider)
        manifest = state.load_manifest()
        warnings = state.check_owner(manifest, project) + state_warnings(env, state)
        remote = scan_remote(env, provider, state)
        opaque = {rel: r.kind for rel, r in remote.items() if r.kind != "file"}
        drift = []
        for rel, entry in sorted(manifest.files.items()):
            r = remote.get(rel)
            why = blocked_reason(rel, opaque)
            if why:
                drift.append((rel, why))
            elif r is None:
                drift.append((rel, "missing on server"))
            elif not stat_matches(entry, r):
                drift.append((rel, "changed on server"))
        return Status(manifest, drift, state.read_lock(), provider.describe(), state.root,
                      warnings)


def history(env: Environment) -> list[DeployRecord]:
    with open_provider(env) as provider:
        return state_for(env, provider).list_history()


def unlock(env: Environment, ui: UI, deploy_id: str | None = None) -> str:
    """Remove the deploy lock, but only the one meant: the one held by `deploy_id`, or the one
    shown to the person who confirmed. Never on age alone, and never a lock that changed in
    between. Returns what happened."""
    with open_provider(env) as provider:
        state = state_for(env, provider)
        held = state.read_lock()
        if held is None:
            return "no lock was held"
        info = LockInfo.parse(held)
        shown = info.describe()
        by_id = (f"`shipr unlock -e {env.name} --id {info.deploy_id}`" if info.deploy_id
                 else f"`shipr unlock -e {env.name}` in a terminal")
        if deploy_id is not None:
            if info.deploy_id != deploy_id:
                raise LockedError(f"the lock is held by {shown}, not by deploy {deploy_id}; "
                                  f"nothing was removed. To remove it, run {by_id}.")
        elif not ui.confirm(f"The deploy lock is held by {shown}.\nRemove it only if that "
                            "deploy is no longer running. Remove it?"):
            raise Aborted(f"lock kept. To remove it without a prompt, run {by_id}.")
        outcome = state.remove_lock(held)
        if outcome == "changed":
            raise LockedError("the lock changed after it was read (its holder finished and "
                              "another deploy took it); nothing was removed. Run `shipr unlock` "
                              "again to see the new holder.")
        if outcome == "missing":
            return f"the lock was already released ({shown})"
        return f"removed lock: {shown}"


def check_connection(env: Environment, trust: bool, ui: UI) -> bool:
    with open_provider(env, trust_new_host=trust, confirm_host_key=ui.trust_host) as provider:
        ui.echo(f"connected to {provider.describe()}")
        for line in provider.diagnostics():
            ui.echo(line)
        state = state_for(env, provider)

        probe = posixpath.join(state.root, ".write-test")
        try:
            created = provider.create_exclusive(probe, "ok")
            if created:
                provider.delete(probe)
            ui.echo(f"write access to {state.root}: ok" if created
                    else f"leftover {probe} exists; remove it")
        except (OSError, ProviderError) as e:
            ui.echo(f"cannot write to {state.root}: {e}")
            return False

        remote = scan_remote(env, provider, state)
        opaque = sorted(rel for rel, r in remote.items() if r.kind != "file")
        ui.echo(f"remote_path {env.remote_path}: {len(remote) - len(opaque)} files found")
        if opaque:
            ui.echo(f"{len(opaque)} symlinks, special files or unreadable directories are "
                    "never touched: " + ", ".join(opaque[:5]) + (" ..." if len(opaque) > 5 else ""))
        try:
            can = remote_exec_available(env, provider)
        except DeployError as e:
            ui.echo(str(e))
            return False
        ui.echo("shell commands: " + ("available" if can else "not available (hooks skipped)"))
        for w in state_warnings(env, state) + [default_location_warning(env, state)]:
            if w:
                ui.echo(f"warning: {w}")
        return True
