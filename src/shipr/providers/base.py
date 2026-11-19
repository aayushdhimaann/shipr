"""The provider contract. The engine only talks to this; SFTP is just one implementation."""

from __future__ import annotations

import posixpath
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

# re-exported: provider authors import these from here
from ..errors import ConnectionLost, ExecNotSupported, ProviderError

__all__ = ["ConnectionLost", "ExecNotSupported", "Provider", "ProviderError", "RemoteStat",
           "tmp_name"]


@dataclass(frozen=True)
class RemoteStat:
    size: int
    mtime: int
    mode: int | None = None  # permission bits only


class Provider(ABC):
    """All paths are absolute POSIX paths on the target."""

    name: ClassVar[str]

    def __init__(self, settings: dict[str, Any], trust_new_host: bool = False):
        self.settings = settings
        self.trust_new_host = trust_new_host
        self.exec_allowed = True  # engine sets False for `ssh_exec: false`

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


def tmp_name(remote: str) -> str:
    d, base = posixpath.split(remote)
    return posixpath.join(d, f".{base}.shipr-tmp")
