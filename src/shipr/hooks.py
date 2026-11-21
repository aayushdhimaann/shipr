"""Run the user's hook and build commands, locally or on the server."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import Environment
from .errors import DeployError
from .providers import Provider
from .ui import UI


def run_local(commands: list[str], cwd: Path, env: dict[str, str], ui: UI) -> None:
    """`env` is the commands' complete environment (workspace.command_env)."""
    for cmd in commands:
        ui.echo(f"$ {cmd}")
        # shell=True on purpose: commands come from the project's own shipr.yml, which is
        # trusted like npm scripts (see docs/security.md)
        r = subprocess.run(cmd, shell=True, cwd=cwd, env=env)
        if r.returncode != 0:
            raise DeployError(f"command failed ({r.returncode}): {cmd}")


def run_remote(provider: Provider, commands: list[str], cwd: str, ui: UI) -> None:
    for cmd in commands:
        ui.echo(f"remote$ {cmd}")
        code, out, err = provider.exec(cmd, cwd=cwd)
        for line in (out + err).rstrip().splitlines():
            ui.echo(f"  {line}")
        if code != 0:
            raise DeployError(f"remote command failed ({code}): {cmd}")


def remote_exec_available(env: Environment, provider: Provider) -> bool:
    """Honour `ssh_exec`: false = never, true = required, auto = probe the server."""
    if env.ssh_exec is False:
        return False
    ok = provider.can_exec()
    if env.ssh_exec is True and not ok:
        raise DeployError(f"ssh_exec is true but {provider.describe()} cannot run commands")
    return ok


def run_remote_phase(provider: Provider, env: Environment, commands: list[str], phase: str,
                     can_exec: bool, ui: UI) -> None:
    if not commands:
        return
    if can_exec:
        run_remote(provider, commands, env.remote_path, ui)
    else:
        ui.warn(f"server has no shell access; {phase} hooks skipped")
