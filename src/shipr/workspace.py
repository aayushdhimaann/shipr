"""Turn a git ref into the exact set of files to deploy: export the commit, run the build,
select and fingerprint the output. Nothing here touches the server."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops
from .config import Config, Environment
from .errors import DeployError, GitError
from .files import LocalFile, Rules, collect
from .hooks import run_local
from .ui import UI


@dataclass
class Workspace:
    repo: Path
    sha: str
    branch: str | None
    subject: str
    project_dir: Path  # inside the export: where hooks/build run
    prefix: str  # output dir relative to the repo root ("." for the root)
    rules: Rules
    files: dict[str, LocalFile] = field(default_factory=dict)
    hook_env: dict[str, str] = field(default_factory=dict)

    def renames_since(self, old_sha: str | None) -> dict[str, str]:
        """Renames between the deployed commit and this one, as deploy-relative paths."""
        if not old_sha or not gitops.has_commit(old_sha, self.repo):
            return {}
        strip = "" if self.prefix == "." else self.prefix + "/"
        return {o[len(strip):]: n[len(strip):]
                for o, n in gitops.renames(old_sha, self.sha, self.repo).items()
                if o.startswith(strip) and n.startswith(strip)}

    def contains(self, old_sha: str) -> bool:
        """False when old_sha is unknown here or not an ancestor (rollback, force-push)."""
        return gitops.has_commit(old_sha, self.repo) and \
            gitops.is_ancestor(old_sha, self.sha, self.repo)


def resolve_target(cfg: Config, env: Environment, ref: str | None) -> tuple[Path, str, str | None]:
    """(repo root, commit sha, branch name) for `ref`, else the env's branch, else HEAD."""
    repo = gitops.repo_root(cfg.dir)
    target = ref or env.branch or "HEAD"
    try:
        sha = gitops.resolve(target, repo)
    except GitError:
        raise DeployError(f"unknown git ref {target!r}") from None
    branch = gitops.current_branch(repo) if target == "HEAD" else target
    return repo, sha, branch


@contextmanager
def prepare(cfg: Config, env: Environment, ref: str | None, ui: UI) -> Iterator[Workspace]:
    """Yield a built Workspace living in a temp dir that is removed afterwards."""
    with tempfile.TemporaryDirectory(prefix="shipr-") as tmp:
        yield _build(cfg, env, ref, ui, Path(tmp))


def _build(cfg: Config, env: Environment, ref: str | None, ui: UI, tmp: Path) -> Workspace:
    repo, sha, branch = resolve_target(cfg, env, ref)
    subject = gitops.subject(sha, repo)
    ui.echo(f"{cfg.project_name} → {env.name}: {ref or env.branch or 'HEAD'} "
            f"@ {sha[:7]} \"{subject}\"")
    if sha == gitops.resolve("HEAD", repo) and gitops.dirty_files(repo):
        ui.warn("you have uncommitted changes; only committed content is deployed")
    if (repo / ".gitmodules").is_file():
        ui.warn("git submodules are not deployed")

    export_root = (tmp / "src").resolve()
    gitops.export(sha, repo, export_root)
    rel_project = os.path.relpath(cfg.dir.resolve(), repo.resolve())
    project_dir = _inside(export_root, export_root / rel_project / env.source,
                          f"source directory {env.source!r} not found in {sha[:7]}")
    # local commands see .env values as environment variables (commands aren't interpolated)
    hook_env = {**cfg.variables(), "SHIPR_ENV": env.name, "SHIPR_COMMIT": sha,
                "SHIPR_PROJECT": cfg.project_name}
    run_local(env.hooks.local_before, project_dir, hook_env, ui)
    run_local(env.build.commands, project_dir, hook_env, ui)
    out_dir = _inside(export_root, project_dir / env.build.output,
                      f"build output {env.build.output!r} does not exist")

    rules = Rules(env.files)
    files = collect(out_dir, rules, warn=ui.warn)
    ui.echo(f"{len(files)} deployable files")
    return Workspace(repo=repo, sha=sha, branch=branch, subject=subject,
                     project_dir=project_dir,
                     prefix=out_dir.relative_to(export_root).as_posix(),
                     rules=rules, files=files, hook_env=hook_env)


def _inside(root: Path, path: Path, missing_msg: str) -> Path:
    """Resolve `path` and require an existing directory within `root` (no ../ escapes)."""
    p = path.resolve()
    if not p.is_dir() or not p.is_relative_to(root):
        raise DeployError(missing_msg)
    return p
