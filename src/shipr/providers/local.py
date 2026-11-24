"""Deploy to a directory on this machine (a mounted share, a local web root, or tests).

Settings: `root` (default "/") — remote paths like /site/index.php map to <root>/site/index.php.
Symlinks, junctions, special files and unreadable directories are handled exactly like the SFTP
provider handles them: reported, never followed or written through.
"""

from __future__ import annotations

import os
import shutil
import stat as statmod
from collections.abc import Callable
from pathlib import Path

from ..proc import run_shell
from .base import Provider, ProviderError, RemoteStat, UnsafePath, tmp_name

# Windows: symlinks *and* directory junctions are reparse points (Path.is_symlink misses
# junctions before Python 3.12)
_REPARSE = getattr(statmod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _kind(st: os.stat_result) -> str:
    if statmod.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & _REPARSE:
        return "symlink"
    if statmod.S_ISDIR(st.st_mode):
        return "dir"
    return "file" if statmod.S_ISREG(st.st_mode) else "special"


def _default_mode(probe: Path) -> int:
    """The mode a new file gets here (umask, default ACLs), from an empty probe file."""
    fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        return os.fstat(fd).st_mode & 0o7777
    finally:
        os.close(fd)
        probe.unlink()


def _sort_entry(p: Path, base: Path, skip: Path | None,
                descend: Callable[[str], bool] | None, out: dict[str, RemoteStat]) -> bool:
    """Put one listed entry into `out` (a file, symlink, special file or unreadable entry);
    True if it is a folder to descend into."""
    rel = p.relative_to(base).as_posix()
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return False  # deleted since the directory was listed
    except OSError:  # listable but not inspectable (e.g. a dir with r but no x)
        out[rel] = RemoteStat(0, 0, kind="unreadable")
        return False
    kind = _kind(st)
    if kind == "dir":
        return p != skip and (descend is None or descend(rel))
    if kind == "file":
        out[rel] = RemoteStat(st.st_size, int(st.st_mtime), st.st_mode & 0o7777)
    else:
        out[rel] = RemoteStat(st.st_size, int(st.st_mtime), kind=kind)
    return False


class LocalProvider(Provider):
    name = "local"

    def connect(self) -> None:
        self.root = Path(self.settings.get("root", "/")).expanduser().resolve()
        if not self.root.is_dir():
            raise ProviderError(f"local root {self.root} does not exist")

    def close(self) -> None:
        pass

    def _p(self, remote: str) -> Path:
        # a backslash or drive colon would be a separator or drive on Windows
        if "\\" in remote or (os.name == "nt" and ":" in remote):
            raise UnsafePath(f"refusing path {remote!r}")
        p = self.root / remote.lstrip("/")
        if p.parts[:len(self.root.parts)] != self.root.parts:
            raise UnsafePath(f"refusing {remote!r}: outside {self.root}")
        return p

    @staticmethod
    def _stat(p: Path) -> RemoteStat:
        st = p.lstat()
        return RemoteStat(st.st_size, int(st.st_mtime), st.st_mode & 0o7777)

    # confinement primitives (checks live in Provider) -----------------------
    def _lkind(self, path: str) -> str | None:
        try:
            return _kind(os.lstat(self._p(path)))
        except (FileNotFoundError, NotADirectoryError):  # a parent is missing or is a file
            return None

    def _mkdir(self, path: str) -> None:
        self._p(path).mkdir()

    def _mkdir_tree(self, path: str) -> None:
        self._p(path).mkdir(parents=True, exist_ok=True)

    def _usable(self, path: str) -> bool:
        """Parents are real directories and `path` is a regular file."""
        return self._check_dirs(path) and self._check_target(path) == "file"

    # files ------------------------------------------------------------------
    def stat(self, path: str) -> RemoteStat | None:
        if not self._check_dirs(path) or self._lkind(path) != "file":
            return None
        return self._stat(self._p(path))

    def walk(self, root: str, skip: str | None = None,
             descend: Callable[[str], bool] | None = None) -> dict[str, RemoteStat]:
        if not self._check_dirs(root):
            return {}
        if self.roots and root not in self.roots and self._lkind(root) not in (None, "dir"):
            raise UnsafePath(f"refusing to list {root}: it is not a directory")
        base = self._p(root)
        skip_p = self._p(skip) if skip else None
        out: dict[str, RemoteStat] = {}

        def unreadable(e: OSError) -> None:
            p = Path(e.filename)
            if p == base:
                if isinstance(e, FileNotFoundError):
                    return
                raise ProviderError(f"cannot list {root}: {e.strerror}")
            out[p.relative_to(base).as_posix()] = RemoteStat(0, 0, kind="unreadable")

        for dirpath, dirnames, filenames in os.walk(base, onerror=unreadable):
            # os.walk lists symlinked dirs in dirnames: never descend into them
            dirnames[:] = [name for name in dirnames + filenames
                           if _sort_entry(Path(dirpath, name), base, skip_p, descend, out)]
        return out

    def upload(self, local: Path, remote: str, mode: int | None = None) -> RemoteStat:
        self._ensure_dirs(remote)
        self._check_target(remote)
        tmp = tmp_name(remote)
        tmp_kind = self._lkind(tmp)
        if tmp_kind == "dir":
            raise UnsafePath(f"refusing {remote}: {tmp} is a directory")
        if tmp_kind is not None:  # a link itself (never its target), or an earlier partial
            self._p(tmp).unlink()
        final = mode if mode is not None else _default_mode(self._p(tmp))
        # created 0600 (O_EXCL: never through a symlink), final mode only once complete
        fd = os.open(self._p(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as out, local.open("rb") as src:
            shutil.copyfileobj(src, out)
        os.chmod(self._p(tmp), final)
        if not self._check_dirs(remote):  # re-check right before the replace
            raise FileNotFoundError(remote)
        self._check_target(remote)
        os.replace(self._p(tmp), self._p(remote))
        return self._stat(self._p(remote))

    def download(self, remote: str, local: Path) -> None:
        if not self._usable(remote):
            raise FileNotFoundError(remote)
        shutil.copyfile(self._p(remote), local)

    def delete(self, path: str) -> None:
        if not self._usable(path):
            raise FileNotFoundError(path)
        self._p(path).unlink()

    def rename(self, src: str, dst: str) -> None:
        if not self._usable(src):
            raise FileNotFoundError(src)
        self._ensure_dirs(dst)
        if not (self._usable(src) and self._check_dirs(dst)):  # re-check both sides
            raise FileNotFoundError(src)
        d = self._p(dst)
        if os.path.lexists(d):
            raise ProviderError(f"{dst} already exists")
        os.rename(self._p(src), d)

    def rmdir_if_empty(self, path: str) -> bool:
        try:
            if not self._check_dirs(path) or self._lkind(path) != "dir":
                return False
            self._p(path).rmdir()
            return True
        except (OSError, UnsafePath):
            return False

    def create_exclusive(self, path: str, text: str) -> bool:
        self._ensure_dirs(path)
        p = self._p(path)
        try:
            with p.open("x", encoding="utf-8") as f:  # O_EXCL: fails on a symlink too
                f.write(text)
            return True
        except FileExistsError:
            self._check_target(path)
            return False

    def supports_exec(self) -> bool:
        return bool(self.settings.get("exec", True))

    def exec(self, command: str, cwd: str | None = None, timeout: float | None = None,
             on_output: Callable[[str], None] | None = None) -> tuple[int, str, str]:
        code, out = run_shell(command, cwd=self._p(cwd) if cwd else None, timeout=timeout,
                              on_output=on_output, capture=on_output is None)
        return code, out, ""

    def describe(self) -> str:
        return f"local:{self.root}"
