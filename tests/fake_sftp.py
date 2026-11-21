"""An in-memory SFTP server for fault-injection tests of SFTPProvider.

It resolves symlinks the way a real server does: every parent component is followed, the final
component is followed by stat/open/put/chmod but not by lstat/remove/rename. That lets a test
observe whether shipr would write *through* a symlink. `before[op]` runs a callback before an
operation, to change the tree at exactly that moment (races, lost replies, crashes).
"""

from __future__ import annotations

import errno
import io
import posixpath
import stat as statmod
from pathlib import Path

from shipr.providers.sftp import SFTPProvider


class Attrs:
    def __init__(self, filename: str, st_mode: int, st_size: int = 0, st_mtime: int = 1):
        self.filename, self.st_mode, self.st_size, self.st_mtime = (
            filename, st_mode, st_size, st_mtime)


class FakeServer:
    def __init__(self, posix_rename: bool = True):
        # path -> ("dir",) | ("file", bytes, mode) | ("link", target) | ("special",)
        self.nodes: dict[str, tuple] = {"/": ("dir",)}
        self.unreadable: set[str] = set()
        self.posix_rename_ok = posix_rename
        self.before: dict[str, object] = {}
        self.calls: list[tuple[str, str]] = []

    # tree helpers for tests ------------------------------------------------
    def mkdirs(self, path: str) -> None:
        parts = path.strip("/").split("/")
        for i in range(1, len(parts) + 1):
            self.nodes.setdefault("/" + "/".join(parts[:i]), ("dir",))

    def file(self, path: str, data: bytes | str = b"", mode: int = 0o644) -> None:
        self.mkdirs(posixpath.dirname(path))
        self.nodes[path] = ("file", data.encode() if isinstance(data, str) else data, mode)

    def link(self, path: str, target: str) -> None:
        self.mkdirs(posixpath.dirname(path))
        self.nodes[path] = ("link", target)

    def swap_for_link(self, path: str, target: str) -> None:
        """Move the directory `path` (and its contents) to `target`, leave a symlink behind."""
        moved = {k: v for k, v in self.nodes.items() if k == path or k.startswith(path + "/")}
        for k, v in moved.items():
            del self.nodes[k]
            self.nodes[target + k[len(path):]] = v
        self.mkdirs(posixpath.dirname(target))
        self.nodes[path] = ("link", target)

    def data(self, path: str) -> bytes | None:
        n = self.nodes.get(path)
        return n[1] if n and n[0] == "file" else None

    # resolution ------------------------------------------------------------
    def _resolve(self, path: str, follow_last: bool) -> str:
        parts = [p for p in path.split("/") if p]
        cur = "/"
        for i, name in enumerate(parts):
            nxt = posixpath.join(cur, name)
            last = i == len(parts) - 1
            for _ in range(20):
                node = self.nodes.get(nxt)
                if node and node[0] == "link" and (follow_last or not last):
                    nxt = node[1]
                else:
                    break
            cur = nxt
        return cur

    def _hook(self, op: str, path: str) -> None:
        self.calls.append((op, path))
        cb = self.before.get(op)
        if cb:
            cb(path)

    @staticmethod
    def _enoent(path: str) -> OSError:
        return FileNotFoundError(errno.ENOENT, "No such file", path)

    def _attrs(self, real: str, name: str) -> Attrs:
        node = self.nodes.get(real)
        if node is None:
            raise self._enoent(real)
        kind = node[0]
        if kind == "dir":
            return Attrs(name, statmod.S_IFDIR | 0o755)
        if kind == "file":
            return Attrs(name, statmod.S_IFREG | node[2], len(node[1]))
        if kind == "link":
            return Attrs(name, statmod.S_IFLNK | 0o777)
        return Attrs(name, statmod.S_IFIFO | 0o644)

    def _parent_dir(self, path: str) -> str:
        parent = self._resolve(posixpath.dirname(path), follow_last=True)
        if self.nodes.get(parent, ("",))[0] != "dir":
            raise self._enoent(path)
        return parent

    # paramiko.SFTPClient surface ---------------------------------------------
    def lstat(self, path):
        self._hook("lstat", path)
        return self._attrs(self._resolve(path, False), posixpath.basename(path))

    def stat(self, path):
        self._hook("stat", path)
        return self._attrs(self._resolve(path, True), posixpath.basename(path))

    def listdir_attr(self, path):
        self._hook("listdir", path)
        real = self._resolve(path, True)
        if self.nodes.get(real, ("",))[0] != "dir":
            raise self._enoent(path)
        if real in self.unreadable:
            raise PermissionError(errno.EACCES, "Permission denied", path)
        out = []
        for k in sorted(self.nodes):
            if k != real and posixpath.dirname(k) == real:
                out.append(self._attrs(k, posixpath.basename(k)))
        return out

    def mkdir(self, path):
        self._hook("mkdir", path)
        real = posixpath.join(self._parent_dir(path), posixpath.basename(path))
        if real in self.nodes:
            raise OSError("Failure")  # what OpenSSH reports for an existing path
        self.nodes[real] = ("dir",)

    def put(self, local, remote, confirm=True):
        self._hook("put", remote)
        real = self._resolve(remote, True)
        self._parent_dir(real)
        self.nodes[real] = ("file", Path(local).read_bytes(), 0o644)

    def get(self, remote, local):
        self._hook("get", remote)
        node = self.nodes.get(self._resolve(remote, True))
        if not node or node[0] != "file":
            raise self._enoent(remote)
        Path(local).write_bytes(node[1])

    def chmod(self, path, mode):
        self._hook("chmod", path)
        real = self._resolve(path, True)
        node = self.nodes[real]
        self.nodes[real] = ("file", node[1], mode)

    def _move(self, src, dst, overwrite):
        s = posixpath.join(self._parent_dir(src), posixpath.basename(src))
        d = posixpath.join(self._parent_dir(dst), posixpath.basename(dst))
        if s not in self.nodes:
            raise self._enoent(src)
        if d in self.nodes and not overwrite:
            raise OSError("Failure")
        self.nodes[d] = self.nodes.pop(s)

    def rename(self, src, dst):
        self._hook("rename", src)
        self._move(src, dst, overwrite=False)

    def posix_rename(self, src, dst):
        self._hook("posix_rename", src)
        if not self.posix_rename_ok:
            raise OSError("Operation unsupported")
        self._move(src, dst, overwrite=True)

    def remove(self, path):
        self._hook("remove", path)
        real = posixpath.join(self._parent_dir(path), posixpath.basename(path))
        node = self.nodes.get(real)
        if node is None:
            raise self._enoent(path)
        if node[0] == "dir":
            raise OSError("Failure")
        del self.nodes[real]

    def rmdir(self, path):
        self._hook("rmdir", path)
        real = posixpath.join(self._parent_dir(path), posixpath.basename(path))
        if self.nodes.get(real, ("",))[0] != "dir" or any(
                k.startswith(real + "/") for k in self.nodes):
            raise OSError("Failure")
        del self.nodes[real]

    def open(self, path, mode="r"):
        self._hook("open", path)
        server = self
        if "x" in mode:
            real = posixpath.join(self._parent_dir(path), posixpath.basename(path))
            if real in self.nodes:  # O_EXCL: also fails on a symlink, without following it
                raise OSError("Failure")

            class W(io.BytesIO):
                def __exit__(self, *a):
                    server.nodes[real] = ("file", self.getvalue(), 0o644)
                    return super().__exit__(*a)
            return W()
        node = self.nodes.get(self._resolve(path, True))
        if not node or node[0] != "file":
            raise self._enoent(path)
        return io.BytesIO(node[1])


def make_provider(server: FakeServer, roots=("/w", "/w/.shipr")) -> SFTPProvider:
    p = SFTPProvider({"host": "h", "username": "u", "retries": 0})
    p._sftp = server
    p._alive = lambda: True  # a raised OSError is a real SFTP error, not a dropped connection
    p.roots = roots
    return p
