"""The provider contract. The engine only talks to this; SFTP is just one implementation."""

from __future__ import annotations

import posixpath
import re
import secrets
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

# re-exported: provider authors import these from here
from ..errors import ConnectionLost, ExecNotSupported, ProviderError, UnsafePath

__all__ = ["ConnectionLost", "ExecNotSupported", "Provider", "ProviderError", "RemoteStat",
           "UnsafePath", "is_within", "leftover_of", "park_name", "tmp_name"]


@dataclass(frozen=True)
class RemoteStat:
    size: int
    mtime: int
    mode: int | None = None  # permission bits only
    # "file", or an entry shipr must never go through: "symlink", "special" (device, fifo,
    # socket) or "unreadable" (a directory it cannot list). Providers that only report regular
    # files can ignore this.
    kind: str = "file"


class Provider(ABC):
    """All paths are absolute POSIX paths on the target."""

    name: ClassVar[str]

    def __init__(self, settings: dict[str, Any], trust_new_host: bool = False):
        self.settings = settings
        self.trust_new_host = trust_new_host
        self.exec_allowed = True  # engine sets False for `ssh_exec: false`
        # engine sets (remote_path, state_path): every path below them must be reached through
        # real directories only (see confined()); empty = no confinement
        self.roots: tuple[str, ...] = ()

    # lifecycle -----------------------------------------------------------
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> Provider:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # files ---------------------------------------------------------------
    @abstractmethod
    def stat(self, path: str) -> RemoteStat | None: ...

    @abstractmethod
    def walk(self, root: str, skip: str | None = None) -> dict[str, RemoteStat]:
        """Files under `root` (recursive) keyed by POSIX path relative to root.

        `skip` is an absolute directory to leave out (shipr's state dir)."""

    @abstractmethod
    def upload(self, local: Path, remote: str, mode: int | None = None) -> RemoteStat:
        """Atomically replace `remote` (write temp file, then rename). Creates parent dirs.
        Returns the remote stat after the write."""

    @abstractmethod
    def download(self, remote: str, local: Path) -> None: ...

    @abstractmethod
    def delete(self, path: str) -> None: ...

    @abstractmethod
    def rename(self, src: str, dst: str) -> None:
        """Move a file, creating dst's parent dirs; dst must not exist."""

    @abstractmethod
    def rmdir_if_empty(self, path: str) -> bool: ...

    @abstractmethod
    def create_exclusive(self, path: str, text: str) -> bool:
        """Create file only if it doesn't exist (used for the deploy lock)."""

    # commands ------------------------------------------------------------
    def can_exec(self) -> bool:
        """Policy first (`ssh_exec: false` sets exec_allowed=False), then target support."""
        return self.exec_allowed and self.supports_exec()

    def supports_exec(self) -> bool:
        """Override: can this target run shell commands? (may probe and cache)"""
        return False

    def exec(self, command: str, cwd: str | None = None,
             timeout: float | None = None) -> tuple[int, str, str]:
        """Run a shell command on the target; returns (exit code, stdout, stderr)."""
        raise ExecNotSupported(f"{self.name} provider cannot run remote commands")

    # generic implementations built on upload/download; override if faster --
    @contextmanager
    def local_copy(self, remote: str) -> Iterator[Path]:
        """Download `remote` into a temp file that is removed afterwards."""
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp, "f")
            self.download(remote, local)
            yield local

    def read_text(self, path: str) -> str | None:
        try:
            with self.local_copy(path) as f:
                return f.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def write_text(self, path: str, text: str) -> None:
        """Atomic (via upload), creating parent dirs."""
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp, "f")
            local.write_text(text, encoding="utf-8")
            self.upload(local, path)

    def copy(self, src: str, dst: str) -> None:
        st = self.stat(src)
        with self.local_copy(src) as f:
            self.upload(f, dst, st.mode if st else None)  # a 0600 file stays 0600

    def sha256(self, paths: list[str]) -> dict[str, str]:
        from ..files import sha256_file

        out = {}
        for p in paths:
            with self.local_copy(p) as f:
                out[p] = sha256_file(f)
        return out

    def describe(self) -> str:
        """Short target description for output and history, e.g. sftp://user@host:22."""
        return self.name

    def diagnostics(self) -> list[str]:
        """Extra lines for `shipr test` (host key, login dir, ...)."""
        return []

    # confinement ---------------------------------------------------------
    def confined(self, path: str) -> list[str]:
        """The directories strictly between `path` and the outermost root containing it,
        outermost first. Each must be a real directory, not a symlink, when `path` is used.

        Containment compares path components, never string prefixes. A non-canonical path
        (`..`, `.`, `//`, relative) is refused rather than normalised: shipr only builds
        canonical paths, so one that isn't means a bug or tampering. No roots: no confinement.
        """
        if not self.roots:
            return []
        parts = _canonical_parts(path)
        best: tuple[str, ...] | None = None
        for root in self.roots:
            rp = _canonical_parts(root)
            if parts[:len(rp)] == rp and (best is None or len(rp) < len(best)):
                best = rp
        if best is None:
            raise UnsafePath(f"refusing {path}: outside {', '.join(self.roots)}")
        return ["/" + "/".join(parts[1:i]) for i in range(len(best) + 1, len(parts))]

    # Built-in providers implement these two (lstat semantics: never follow the last
    # component) and get the checks below; third-party providers may ignore them.
    def _lkind(self, path: str) -> str | None:
        """'file', 'dir', 'symlink' or 'special' without following symlinks; None if missing."""
        raise NotImplementedError

    def _mkdir(self, path: str) -> None:
        raise NotImplementedError

    def _mkdir_tree(self, path: str) -> None:
        """Create `path` and missing parents, following symlinks: only used for a configured
        root and the directories above it, which are the user's own choice."""
        raise NotImplementedError

    def _check_dirs(self, path: str) -> bool:
        """Fresh lstat of every confined directory of `path`. False if one is missing (so
        `path` can't exist); UnsafePath if one is a symlink or anything but a directory."""
        for d in self.confined(path):
            kind = self._lkind(d)
            if kind is None:
                return False
            if kind != "dir":
                raise UnsafePath(f"refusing {path}: {d} is a {kind}, not a directory "
                                 "(shipr never goes through symlinks on the server)")
        return True

    def _ensure_dirs(self, path: str) -> None:
        """Create the missing directories of `path`, re-checking each one after creating it:
        a directory swapped for a symlink right after mkdir is caught before it is used."""
        dirs = self.confined(path)
        self._mkdir_tree(posixpath.dirname(dirs[0] if dirs else path))
        for d in dirs:
            kind = self._lkind(d)
            if kind is None:
                try:
                    self._mkdir(d)
                except OSError:
                    if self._lkind(d) is None:
                        raise  # a real failure (permissions, disk), not a concurrent mkdir
                kind = self._lkind(d)
            if kind != "dir":
                raise UnsafePath(f"refusing {path}: {d} is a {kind}, not a directory "
                                 "(shipr never goes through symlinks on the server)")

    def _check_target(self, path: str) -> str | None:
        """The final component must be absent or a regular file (never a symlink)."""
        kind = self._lkind(path)
        if kind not in (None, "file"):
            raise UnsafePath(f"refusing {path}: it is a {kind}, not a regular file")
        return kind


def _canonical_parts(path: str) -> tuple[str, ...]:
    if (not path.startswith("/") or path.startswith("//") or "\\" in path or "\0" in path
            or posixpath.normpath(path) != path):
        raise UnsafePath(f"refusing non-canonical remote path {path!r}")
    return PurePosixPath(path).parts


def is_within(path: str, root: str) -> bool:
    """`path` is `root` or below it, comparing components (so /a/bc is not within /a/b)."""
    p, r = PurePosixPath(path).parts, PurePosixPath(root).parts
    return p[:len(r)] == r


def tmp_name(remote: str) -> str:
    d, base = posixpath.split(remote)
    return posixpath.join(d, f".{base}.shipr-tmp")


def park_name(remote: str) -> str:
    """Where a non-atomic swap parks the old file: unique, so a leftover from an earlier crash
    can never block (or be overwritten by) a later swap."""
    d, base = posixpath.split(remote)
    return posixpath.join(d, f".{base}.{secrets.token_hex(4)}.shipr-old")


_PARK = re.compile(r"\.(.+)\.[0-9a-f]{8}\.shipr-old")


def leftover_of(path: str) -> tuple[str, str] | None:
    """("tmp" | "park", path of the file it belongs to) for shipr's own leftover names."""
    d, name = posixpath.split(path)
    m = _PARK.fullmatch(name)
    if m:
        return "park", posixpath.join(d, m.group(1))
    if name.startswith(".") and name.endswith(".shipr-tmp") and len(name) > len("..shipr-tmp"):
        return "tmp", posixpath.join(d, name[1:-len(".shipr-tmp")])
    if name.endswith(".shipr-old") and len(name) > len(".shipr-old"):
        return "park", posixpath.join(d, name[:-len(".shipr-old")])  # pre-0.2 name
    return None
