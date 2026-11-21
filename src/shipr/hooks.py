"""Run the user's hook and build commands, locally or on the server."""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

from .config import Environment
from .errors import DeployError
from .proc import run_shell
from .providers import Provider
from .ui import UI


def run_local(commands: list[str], cwd: Path, env: dict[str, str], ui: UI,
              timeout: float | None = None) -> None:
    """`env` is the commands' complete environment (workspace.command_env). With a timeout
    (hooks), each command runs non-interactively in its own process group, so a timeout or
    Ctrl-C stops it and its children; without one (builds), exactly as typed in a terminal."""
    for cmd in commands:
        ui.echo(f"$ {cmd}")
        # shell=True on purpose: commands come from the project's own shipr.yml, which is
        # trusted like npm scripts (see docs/security.md)
        if timeout is None:
            code = subprocess.run(cmd, shell=True, cwd=cwd, env=env).returncode
        else:
            code, _ = run_shell(cmd, cwd=cwd, env=env, timeout=timeout)
        if code != 0:
            raise DeployError(f"command failed ({code}): {cmd}")


def run_remote(provider: Provider, commands: list[str], cwd: str, ui: UI,
               timeout: float | None = None) -> None:
    """Run each command once (never retried: a hook may not be safe to repeat), showing its
    output as it arrives. Providers written before streaming show it when the command ends."""
    streams = _accepts(provider.exec, "on_output")
    for cmd in commands:
        ui.echo(f"remote$ {cmd}")

        def show(line: str) -> None:
            ui.echo(f"  {line}")
        if streams:
            code, out, err = provider.exec(cmd, cwd=cwd, timeout=timeout, on_output=show)
        else:
            code, out, err = provider.exec(cmd, cwd=cwd, timeout=timeout)
        for line in (out + err).rstrip().splitlines():
            show(line)
        if code != 0:
            raise DeployError(f"remote command failed ({code}): {cmd}")


def _accepts(fn: object, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters.values()  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind is p.VAR_KEYWORD for p in params)


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
        run_remote(provider, commands, env.remote_path, ui, timeout=env.hooks.timeout)
    else:
        ui.warn(f"server has no shell access; {phase} hooks skipped")
