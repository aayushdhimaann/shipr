"""Read-only and maintenance operations: status, history, unlock, connection check."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from .config import Environment
from .engine import open_provider, scan_remote, state_for
from .errors import DeployError, ProviderError
from .hooks import remote_exec_available
from .models import DeployRecord, Manifest
from .planner import stat_matches
from .ui import UI


@dataclass
class Status:
    manifest: Manifest
    drift: list[tuple[str, str]]  # (path, why)
    lock: str | None
    target: str


def status(env: Environment) -> Status:
    with open_provider(env) as provider:
        state = state_for(env, provider)
        manifest = state.load_manifest()
        remote = scan_remote(env, provider, state)
        drift = []
        for rel, entry in sorted(manifest.files.items()):
            r = remote.get(rel)
            if r is None:
                drift.append((rel, "missing on server"))
            elif not stat_matches(entry, r):
                drift.append((rel, "changed on server"))
        return Status(manifest, drift, state.read_lock(), provider.describe())


def history(env: Environment) -> list[DeployRecord]:
    with open_provider(env) as provider:
        return state_for(env, provider).list_history()


def unlock(env: Environment) -> str | None:
    """Remove the deploy lock; returns who held it."""
    with open_provider(env) as provider:
        state = state_for(env, provider)
        info = state.read_lock()
        state.release_lock()
        return info


def check_connection(env: Environment, trust: bool, ui: UI) -> bool:
    with open_provider(env, trust_new_host=trust) as provider:
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

        n = len(scan_remote(env, provider, state))
        ui.echo(f"remote_path {env.remote_path}: {n} files found")
        try:
            can = remote_exec_available(env, provider)
        except DeployError as e:
            ui.echo(str(e))
            return False
        ui.echo("shell commands: " + ("available" if can else "not available (hooks skipped)"))
        if state.inside_webroot:
            ui.echo(f"note: deploy records live inside the web root ({state.root}); shipr blocks "
                    "them for Apache via .htaccess. On nginx, deny it or set `state_path` "
                    "outside the web root.")
        return True
