"""Deploy to a directory on this machine (a mounted share, a local web root, or tests).

Settings: `root` (default "/") — remote paths like /site/index.php map to <root>/site/index.php.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .base import Provider, ProviderError, RemoteStat, tmp_name


class LocalProvider(Provider):
    name = "local"

    def connect(self) -> None:
        self.root = Path(self.settings.get("root", "/")).expanduser().resolve()
        if not self.root.is_dir():
            raise ProviderError(f"local root {self.root} does not exist")

    def close(self) -> None:
        pass

    def _p(self, remote: str) -> Path:
        return self.root / remote.lstrip("/")

    @staticmethod
    def _stat(p: Path) -> RemoteStat:
        st = p.stat()
        return RemoteStat(st.st_size, int(st.st_mtime), st.st_mode & 0o7777)

    def stat(self, path: str) -> RemoteStat | None:
        p = self._p(path)
        return self._stat(p) if p.is_file() else None

    def walk(self, root: str, skip: str | None = None) -> dict[str, RemoteStat]:
        base = self._p(root)
        skip_p = self._p(skip) if skip else None
        out: dict[str, RemoteStat] = {}
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if Path(dirpath, d) != skip_p]
            for f in filenames:
                p = Path(dirpath, f)
                if p.is_file():
                    out[p.relative_to(base).as_posix()] = self._stat(p)
        return out

    def upload(self, local: Path, remote: str, mode: int | None = None) -> RemoteStat:
        dst = self._p(remote)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._p(tmp_name(remote))
        shutil.copyfile(local, tmp)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, dst)
        return self._stat(dst)

    def download(self, remote: str, local: Path) -> None:
        shutil.copyfile(self._p(remote), local)

    def delete(self, path: str) -> None:
        self._p(path).unlink()

    def rename(self, src: str, dst: str) -> None:
        d = self._p(dst)
        if d.exists():
            raise ProviderError(f"{dst} already exists")
        d.parent.mkdir(parents=True, exist_ok=True)
        os.rename(self._p(src), d)

    def rmdir_if_empty(self, path: str) -> bool:
        try:
            self._p(path).rmdir()
            return True
        except OSError:
            return False

    def create_exclusive(self, path: str, text: str) -> bool:
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            with p.open("x", encoding="utf-8") as f:
                f.write(text)
            return True
        except FileExistsError:
            return False

    def supports_exec(self) -> bool:
        return bool(self.settings.get("exec", True))

    def exec(self, command: str, cwd: str | None = None,
             timeout: float | None = None) -> tuple[int, str, str]:
        r = subprocess.run(command, shell=True, cwd=self._p(cwd) if cwd else None,
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr

    def describe(self) -> str:
        return f"local:{self.root}"
