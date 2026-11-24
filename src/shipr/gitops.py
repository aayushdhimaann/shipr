"""Thin wrapper over the git CLI (git is a hard requirement, so no GitPython)."""

from __future__ import annotations

import hashlib
import platform
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


def blobs(rev: str, cwd: Path) -> dict[str, str]:
    """Repo-relative path -> git blob id of every file in `rev`."""
    out = {}
    for entry in git("ls-tree", "-r", "-z", "--full-tree", rev, cwd=cwd).split("\0"):
        meta, _, path = entry.partition("\t")
        if meta.split(" ")[1:2] == ["blob"]:
            out[path] = meta.split(" ")[2]
    return out


def blob_id(path: Path) -> str:
    """The id git gives this file's content."""
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


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
    _require_extraction_filter()  # before any work
    archive = dest.parent / f"{dest.name}.tar"
    git("-c", "core.autocrlf=false", "archive", "--format=tar", "-o", str(archive), sha,
        cwd=repo)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        extract(archive, dest)
    except GitError as e:
        raise GitError(f"cannot export {sha[:7]}: {e}") from None
    finally:
        archive.unlink(missing_ok=True)


def extract(archive: Path, dest: Path) -> None:
    """Extract with tarfile's "data" filter: no absolute or ../ paths, no links leading out of
    `dest`, no device files, no setuid bits. There is deliberately no unfiltered fallback."""
    _require_extraction_filter()
    try:
        with tarfile.open(archive) as tar:
            tar.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as e:
        raise GitError(f"{e} (absolute or escaping symlink?)") from None


def _require_extraction_filter() -> None:
    # Python 3.10.0-3.10.11 and 3.11.0-3.11.3 lack it (their extractall() has no `filter`);
    # feature-detected, so a distribution build with the backport works too
    if not hasattr(tarfile, "data_filter"):
        raise GitError(
            f"this Python ({platform.python_version()}) can't extract the git export safely: "
            "shipr needs Python 3.10.12 or later, 3.11.4 or later, or 3.12+ (tarfile "
            "extraction filters). Upgrade Python, then reinstall shipr.")
