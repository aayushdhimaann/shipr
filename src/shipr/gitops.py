"""Thin wrapper over the git CLI (git is a hard requirement, so no GitPython)."""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

from .errors import GitError


def _run(args: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        raise GitError("git is not installed or not on PATH") from None


def git(*args: str, cwd: Path, check: bool = True) -> str:
    """Run git and return stdout; raise GitError on failure when `check`."""
    r = _run(args, cwd)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def repo_root(path: Path) -> Path:
    return Path(git("rev-parse", "--show-toplevel", cwd=path))


def resolve(ref: str, cwd: Path) -> str:
    return git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=cwd)


def current_branch(cwd: Path) -> str | None:
    name = git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd, check=False)
    return None if name in ("", "HEAD") else name


def branch_at(ref: str, sha: str, cwd: Path) -> str | None:
    """`ref` if it names a local or remote-tracking branch pointing at `sha`, else None."""
    for full in (f"refs/heads/{ref}", f"refs/remotes/{ref}"):
        if git("rev-parse", "--verify", "--quiet", f"{full}^{{commit}}", cwd=cwd,
               check=False) == sha:
            return ref
    return None


def dirty_files(cwd: Path) -> list[str]:
    out = git("status", "--porcelain", "--untracked-files=no", cwd=cwd)
    return [line[3:] for line in out.splitlines() if line]


def subject(sha: str, cwd: Path) -> str:
    return git("log", "-1", "--format=%s", sha, cwd=cwd)


def is_ancestor(old: str, new: str, cwd: Path) -> bool:
    return _run(("merge-base", "--is-ancestor", old, new), cwd).returncode == 0


def has_commit(sha: str, cwd: Path) -> bool:
    return git("cat-file", "-t", sha, cwd=cwd, check=False) == "commit"


def renames(old: str, new: str, cwd: Path) -> dict[str, str]:
    """Old path -> new path for renames between two commits (display hint only)."""
    out = git("diff", "--name-status", "-M", old, new, cwd=cwd, check=False)
    pairs = (line.split("\t") for line in out.splitlines() if line.startswith("R"))
    return {p[1]: p[2] for p in pairs if len(p) == 3}


def export(sha: str, repo: Path, dest: Path) -> None:
    """Write the committed tree of `sha` into `dest` (no .git, honours export-ignore).

    core.autocrlf is forced off so Windows checkouts upload the same bytes as Linux ones.
    """
    archive = dest.parent / f"{dest.name}.tar"
    git("-c", "core.autocrlf=false", "archive", "--format=tar", "-o", str(archive), sha,
        cwd=repo)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive) as tar:
            tar.extractall(dest, filter="data")  # blocks path traversal, device files, etc.
    except (tarfile.TarError, OSError) as e:
        raise GitError(f"cannot export {sha[:7]}: {e} (absolute or escaping symlink?)") from None
    finally:
        archive.unlink(missing_ok=True)
