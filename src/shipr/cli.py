"""`shipr` command line: argument parsing and presentation only. All behaviour lives in
engine (deploy, rollback) and admin (status, history, unlock, test)."""

from __future__ import annotations

import dataclasses
import signal
import sys
from pathlib import Path

import click

from . import __version__, admin, config, engine, gitops, scaffold, workspace
from .errors import GitError, ShiprError
from .models import DeployRecord
from .state import LockInfo
from .ui import UI

# 2 is click's usage-error code, so a partial deploy uses 4
EXIT_ABORTED, EXIT_PARTIAL, EXIT_INTERRUPTED = 3, 4, 130


class _Failure(click.ClickException):
    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


class ShiprGroup(click.Group):
    """Turns expected failures into clean messages + exit codes (no tracebacks)."""

    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except ShiprError as e:
            raise _Failure(str(e), e.exit_code) from e
        except KeyboardInterrupt:
            raise _Failure("interrupted: progress was saved; run the command again to finish",
                           EXIT_INTERRUPTED) from None


def make_ui() -> UI:
    def confirm(msg: str) -> bool:
        if not sys.stdin.isatty():
            click.echo(msg)
            click.echo("not a terminal: refusing to guess; pass --yes to confirm", err=True)
            return False
        return click.confirm(msg, default=False)

    def trust_host(host: str, key: str) -> bool:
        if not sys.stdin.isatty():  # CI: the error that follows explains how to pin host_key
            return False
        return click.confirm(
            f"The authenticity of host {host} can't be established. It presented:\n  {key}\n"
            "Compare this with the server's real fingerprint (ask your provider, or run "
            "`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the server).\n"
            "Trust it and remember it?", default=False)
    return UI(echo=click.echo, confirm=confirm, trust_host=trust_host)


class Ctx:
    def __init__(self, config_path: Path | None):
        self.config_path = config_path

    def cfg(self) -> config.Config:
        return config.load(self.config_path)

    def env(self, name: str | None) -> tuple[config.Config, config.Environment]:
        cfg = self.cfg()
        if name is None:
            if len(cfg.environments) != 1:
                raise click.UsageError(f"choose an environment with -e "
                                       f"({', '.join(cfg.environments)})")
            name = next(iter(cfg.environments))
        env = cfg.env(name)
        for w in cfg.state_overlaps(name):  # raises when two envs certainly share records
            click.echo(f"warning: {w}")
        return cfg, env


env_option = click.option("-e", "--env", "env_name", help="Environment name from shipr.yml.")
yes_option = click.option("--yes", "-y", is_flag=True,
                          help="Answer yes to every confirmation (needed when not in a terminal).")
dry_run_option = click.option("--dry-run", "-n", is_flag=True,
                              help="Show what would change; change nothing.")
no_delete_option = click.option("--no-delete", is_flag=True,
                                help="Never delete remote files in this run.")
trust_option = click.option("--trust", is_flag=True,
                            help="Accept and remember an unknown server host key.")


@click.group(cls=ShiprGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="shipr")
@click.option("-c", "--config", "config_path", type=click.Path(path_type=Path, dir_okay=False),
              help="Path to shipr.yml (default: search upward from the current directory).")
@click.pass_context
def cli(ctx: click.Context, config_path: Path | None) -> None:
    """Deploy an exact Git commit to your servers, safely."""
    ctx.obj = Ctx(config_path)


@cli.command()
@click.option("--force", is_flag=True, help="Overwrite an existing shipr.yml.")
def init(force: bool) -> None:
    """Create a shipr.yml template in the current directory."""
    root = Path.cwd()
    target = root / config.CONFIG_NAME
    if target.exists() and not force:
        raise click.ClickException(f"{target} already exists (use --force to overwrite)")
    target.write_text(scaffold.render(root), encoding="utf-8")
    click.echo(f"wrote {target}. Edit hosts and paths, then run `shipr validate`.")
    gi = root / ".gitignore"
    if ".env" not in (gi.read_text(encoding="utf-8") if gi.is_file() else ""):
        click.echo("tip: add .env to .gitignore before putting passwords in it")


@cli.command()
@click.pass_obj
def validate(obj: Ctx) -> None:
    """Check shipr.yml for every environment."""
    cfg = obj.cfg()
    warnings: list[str] = []
    for name in cfg.environments:
        env = cfg.env(name)
        warnings += [w for w in cfg.state_overlaps(name) if w not in warnings]
        click.echo(f"{name}: ok ({env.provider} → {env.remote_path})")
        if w := workspace.credential_warning(cfg, env):
            warnings.append(f"{name}: {w}")
        if not env.files.default_excludes:
            found = workspace.committed_default_excluded(cfg, env)
            built = found is not None and env.build.commands
            warnings.append(f"{name}: " + workspace.default_excludes_warning(
                found, f"committed at {env.branch or 'HEAD'}") + (
                " (files your build creates are listed when you deploy; try --dry-run)"
                if built else ""))
    for w in warnings:
        click.echo(f"warning: {w}")


@cli.command("test")
@env_option
@trust_option
@click.pass_obj
def test_cmd(obj: Ctx, env_name: str | None, trust: bool) -> None:
    """Check the connection, permissions and shell access for an environment."""
    _, env = obj.env(env_name)
    if not admin.check_connection(env, trust, make_ui()):
        sys.exit(1)


def _exit_for(record: DeployRecord) -> None:
    if record.status == "partial":
        sys.exit(EXIT_PARTIAL)


@cli.command()
@env_option
@click.option("--ref", help="Branch, tag or commit to deploy (default: env branch, else HEAD).")
@dry_run_option
@yes_option
@no_delete_option
@trust_option
@click.pass_obj
def deploy(obj: Ctx, env_name, ref, dry_run, yes, no_delete, trust) -> None:
    """Deploy a commit to an environment."""
    cfg, env = obj.env(env_name)
    opts = engine.DeployOptions(dry_run=dry_run, yes=yes, no_delete=no_delete,
                                trust_new_host=trust)
    _exit_for(engine.deploy(cfg, env, ref, opts, make_ui()))


@cli.command()
@env_option
@click.option("--to", help="Deploy id or commit to restore (default: the previous deploy).")
@dry_run_option
@yes_option
@no_delete_option
@click.pass_obj
def rollback(obj: Ctx, env_name, to, dry_run, yes, no_delete) -> None:
    """Redeploy the previously deployed commit (only differing files change)."""
    cfg, env = obj.env(env_name)
    opts = engine.DeployOptions(dry_run=dry_run, yes=yes, no_delete=no_delete)
    _exit_for(engine.rollback(cfg, env, to, opts, make_ui()))


@cli.command()
@env_option
@click.pass_obj
def status(obj: Ctx, env_name) -> None:
    """Show what is deployed and whether the server drifted."""
    cfg, env = obj.env(env_name)
    s = admin.status(env, cfg.project_name)
    m = s.manifest
    for w in s.warnings:
        click.echo(f"warning: {w}")
    if not m.deployed_commit:
        click.echo(f"{env.name}: nothing deployed by shipr yet ({s.target})")
    else:
        click.echo(f"{env.name}: {m.deployed_commit[:7]}"
                   f"{f' ({m.branch})' if m.branch else ''} deployed "
                   f"{m.deployed_at}, {len(m.files)} files tracked")
        target = env.branch or "HEAD"
        try:
            sha = gitops.resolve(target, gitops.repo_root(cfg.dir))
            state = "up to date" if sha == m.deployed_commit else "not deployed"
            click.echo(f"{target} is at {sha[:7]} ({state})")
        except GitError:
            pass  # not in a repo / ref missing locally: the server-side status is still useful
    default = "" if env.state_path else " (default location beside remote_path; set " \
        "state_path if your web server could serve it)"
    click.echo(f"records: {s.state_root}{default}")
    if m.in_progress:
        click.echo(f"warning: interrupted deploy {m.in_progress.deploy_id}")
    if m.hooks_pending:
        click.echo("warning: remote hooks of the deployed commit have not all run; the next "
                   "deploy runs them")
    if s.lock:
        click.echo(f"locked: {LockInfo.parse(s.lock).describe()}")
    for rel, why in s.drift:
        click.echo(f"  drift  {rel}  ({why})")
    if not s.drift:
        click.echo("no drift: server files match the last deploy")


@cli.command()
@env_option
@click.option("--show", "show_id", help="Print one deploy record in full (JSON).")
@click.option("-n", "limit", default=20, show_default=True, help="How many recent deploys to list.")
@click.pass_obj
def history(obj: Ctx, env_name, show_id, limit) -> None:
    """List previous deploys."""
    _, env = obj.env(env_name)
    records = admin.history(env)
    if show_id:
        match = next((r for r in records if r.id == show_id), None)
        if match is None:
            raise click.ClickException(f"no deploy {show_id!r}")
        click.echo(match.model_dump_json(indent=2))
        return
    if not records:
        click.echo("no deploys yet")
    for r in records[-limit:]:
        click.echo(f"{r.id}  {r.kind:8} {r.status:11} {r.commit[:7]} {r.branch or '-':12} "
                   f"↑{len(r.uploaded)} ✗{len(r.deleted)} !{len(r.failed)}  {r.by}")


@cli.command()
@env_option
@click.option("--id", "deploy_id", metavar="DEPLOY_ID",
              help="Remove the lock only if this deploy holds it (no prompt; for scripts).")
@click.option("--yes", "-y", is_flag=True,
              help="Not enough on its own: shipr never removes a lock nobody has seen. "
                   "Use --id.")
@click.pass_obj
def unlock(obj: Ctx, env_name, deploy_id, yes) -> None:
    """Remove a stale deploy lock (only when no deploy is running). Shows who holds it
    first, and removes only that lock."""
    _, env = obj.env(env_name)
    ui = make_ui()
    if yes or not sys.stdin.isatty():
        def show_only(msg: str) -> bool:
            click.echo(msg.splitlines()[0])
            return False
        ui = dataclasses.replace(ui, confirm=show_only)
    click.echo(admin.unlock(env, ui, deploy_id))


def _sigterm(_signum, _frame):
    # treat `kill` like Ctrl-C so progress is checkpointed and the lock released
    raise KeyboardInterrupt


def main() -> None:
    signal.signal(signal.SIGTERM, _sigterm)
    cli()


if __name__ == "__main__":
    main()
