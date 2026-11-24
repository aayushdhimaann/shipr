"""Select the files to deploy (include/exclude/protect globs) and fingerprint them."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from pathspec import GitIgnoreSpec

from .config import Files

# Never upload secrets, VCS data or shipr's own files unless `default_excludes: false`.
DEFAULT_EXCLUDES = [
    ".git/", ".shipr/", ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*",
    "shipr.yml", "node_modules/", ".DS_Store", "Thumbs.db", "*.log", "*.shipr-tmp",
    "*.shipr-old",
]
_DEFAULTS = GitIgnoreSpec.from_lines(DEFAULT_EXCLUDES)


def default_excluded(paths: Iterable[str]) -> list[str]:
    """The paths the built-in excludes would keep off the server (names only, sorted)."""
    return sorted(p for p in paths if _DEFAULTS.match_file(p))


@dataclass(frozen=True)
class LocalFile:
    path: Path
    sha256: str
    size: int
    mode: int = 0o644  # permission bits used when the file is new on the server


class Rules:
    def __init__(self, files: Files):
        self.include = GitIgnoreSpec.from_lines(files.include)
        self.exclude = GitIgnoreSpec.from_lines(
            (DEFAULT_EXCLUDES if files.default_excludes else []) + files.exclude
        )
        self.protect = GitIgnoreSpec.from_lines(files.protect)

    def wanted(self, rel: str) -> bool:
        return self.include.match_file(rel) and not self.exclude.match_file(rel)

    def dir_excluded(self, rel: str) -> bool:
        return self.exclude.match_file(rel + "/")

    def protected(self, rel: str) -> bool:
        return self.protect.match_file(rel)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _links_to_file_inside(link: Path, root: Path) -> bool:
    target = link.resolve()
    return target.is_file() and target.is_relative_to(root)


def collect(root: Path, rules: Rules,
            warn: Callable[[str], None] = print) -> dict[str, LocalFile]:
    """Map POSIX relative path -> LocalFile for every deployable file under `root`."""
    root = root.resolve()
    out: dict[str, LocalFile] = {}
    for dirpath, dirnames, filenames in os.walk(root):  # followlinks=False: no dir symlinks
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = sorted(d for d in dirnames if not rules.dir_excluded(rel_dir + d))
        for name in sorted(filenames):
            rel = rel_dir + name
            if not rules.wanted(rel):
                continue
            p = Path(dirpath, name)
            if p.is_symlink() and not _links_to_file_inside(p, root):
                warn(f"skipping symlink {rel} (points outside the project)")
                continue
            st = p.stat()
            # keep the executable bit, drop group/other write (build output may be 0664)
            out[rel] = LocalFile(p, sha256_file(p), st.st_size, st.st_mode & 0o755)
    return out
