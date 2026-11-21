"""SFTP provider (paramiko).

Settings: host, port=22, username, password | key_file [+ passphrase] (neither: ssh-agent),
timeout=15, retries=3, known_hosts (extra file, optional). Host keys are always verified;
unknown hosts are only accepted with `trust_new_host` (shipr test --trust), and saved to
shipr's own known_hosts.
"""

from __future__ import annotations

import posixpath
import shlex
import socket
import stat as statmod
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import paramiko
from pydantic import BaseModel, ConfigDict, ValidationError

from ..errors import ConnectionLost, ProviderError, UnsafePath
from ..paths import app_dir
from .base import Provider, RemoteStat, park_name, tmp_name

T = TypeVar("T")
TRANSIENT = (paramiko.SSHException, EOFError, socket.timeout, ConnectionError)
SHA256_BATCH = 200


class SFTPSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    host: str
    port: int = 22
    username: str
    password: str | None = None
    key_file: str | None = None
    passphrase: str | None = None
    timeout: float = 15
    retries: int = 3
    known_hosts: str | None = None


class _TrustOnFirstUse(paramiko.MissingHostKeyPolicy):
    """Accept an unknown host key once and remember it in shipr's own known_hosts."""

    def __init__(self, store: Path):
        self.store = store

    def missing_host_key(self, client, hostname, key):
        client.get_host_keys().add(hostname, key.get_name(), key)
        self.store.parent.mkdir(parents=True, exist_ok=True)
        with self.store.open("a", encoding="utf-8") as f:
            f.write(f"{hostname} {key.get_name()} {key.get_base64()}\n")


def _remote_stat(a: paramiko.SFTPAttributes) -> RemoteStat:
    return RemoteStat(a.st_size or 0, int(a.st_mtime or 0),
                      statmod.S_IMODE(a.st_mode) if a.st_mode else None)


def _kind(a: paramiko.SFTPAttributes) -> str:
    mode = a.st_mode or 0
    if statmod.S_ISLNK(mode):
        return "symlink"
    if statmod.S_ISDIR(mode):
        return "dir"
    return "file" if statmod.S_ISREG(mode) else "special"


def _attrs(sftp: paramiko.SFTPClient, path: str) -> paramiko.SFTPAttributes | None:
    """stat() that returns None for a missing path (follows symlinks: only used at or above a
    configured root)."""
    try:
        return sftp.stat(path)
    except FileNotFoundError:
        return None


class SFTPProvider(Provider):
    name = "sftp"

    def __init__(self, settings: dict[str, Any], trust_new_host: bool = False):
        super().__init__(settings, trust_new_host)
        try:
            self.cfg = SFTPSettings.model_validate(settings)
        except ValidationError as e:
            raise ProviderError(f"invalid sftp settings:\n{e}") from None
        self.client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None
        self._exec_ok: bool | None = None
        self.host_fingerprint = ""

    # connection ------------------------------------------------------------
    def connect(self) -> None:
        last: Exception | None = None
        for attempt in range(self.cfg.retries + 1):
            try:
                self._connect_once()
                return
            except paramiko.AuthenticationException:
                raise ProviderError(f"authentication failed for {self.describe()}") from None
            except paramiko.BadHostKeyException as e:
                raise ProviderError(
                    f"HOST KEY MISMATCH for {e.hostname}: the server's key changed. This can mean "
                    "a man-in-the-middle attack. Verify it, then fix your known_hosts.") from None
            except paramiko.SSHException as e:
                if "not found in known_hosts" in str(e):
                    raise ProviderError(
                        f"{self.cfg.host} is not a known host. Run `shipr test -e <env> --trust` "
                        "once to verify and store its key.") from None
                last = e
            except (OSError, EOFError) as e:
                last = e
            if attempt < self.cfg.retries:
                self._backoff(attempt)
        raise ProviderError(f"could not connect to {self.describe()}: {last}")

    def _connect_once(self) -> None:
        self.close()
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        own = app_dir() / "known_hosts"
        extra = Path(self.cfg.known_hosts).expanduser() if self.cfg.known_hosts else None
        for f in (own, extra):
            if f and f.is_file():
                client.load_host_keys(str(f))
        client.set_missing_host_key_policy(
            _TrustOnFirstUse(own) if self.trust_new_host else paramiko.RejectPolicy())
        client.connect(
            self.cfg.host, port=self.cfg.port, username=self.cfg.username,
            password=self.cfg.password,
            key_filename=str(Path(self.cfg.key_file).expanduser()) if self.cfg.key_file else None,
            passphrase=self.cfg.passphrase, timeout=self.cfg.timeout,
            banner_timeout=self.cfg.timeout, auth_timeout=self.cfg.timeout,
            allow_agent=not self.cfg.password, look_for_keys=False,
        )
        key = client.get_transport().get_remote_server_key()
        self.host_fingerprint = f"{key.get_name()} SHA256:{key.fingerprint}"
        self.client, self._sftp = client, client.open_sftp()
        self._sftp.get_channel().settimeout(max(self.cfg.timeout, 30))

    def close(self) -> None:
        if self.client:
            self.client.close()
        self.client = self._sftp = None

    def _alive(self) -> bool:
        t = self.client.get_transport() if self.client else None
        return bool(t and t.is_active())

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(min(2 ** attempt, 10))

    def _retry(self, op: Callable[[paramiko.SFTPClient], T]) -> T:
        """Run `op(sftp)`; if the connection drops, reconnect and run it again.

        `op` receives the *current* client on each attempt, so nothing keeps a handle to a
        dead connection across a reconnect.
        """
        for attempt in range(self.cfg.retries + 1):
            try:
                if self._sftp is None:
                    raise ConnectionError("not connected")
                return op(self._sftp)
            except (*TRANSIENT, OSError) as e:
                # A dead transport surfaces as plain OSError("Socket is closed"); real SFTP
                # errors (permission denied, no such file) leave the transport alive.
                if not isinstance(e, TRANSIENT) and self._alive():
                    raise
                if attempt == self.cfg.retries:
                    raise ConnectionLost(f"connection to {self.describe()} lost: {e}") from e
                self._backoff(attempt)
                try:
                    self._connect_once()
                except (*TRANSIENT, OSError):
                    pass  # the next attempt fails fast and counts against retries
        raise AssertionError("unreachable")

    # confinement primitives (checks live in Provider) ---------------------------
    # SFTP is path-based: there is no O_NOFOLLOW or openat, so every check below is a fresh
    # lstat made right before the request it guards. A process that can write to the tree can
    # still swap a symlink in between the two (see docs/security.md).
    def _lkind(self, path: str) -> str | None:
        try:
            return _kind(self._sftp.lstat(path))
        except FileNotFoundError:
            return None

    def _mkdir(self, path: str) -> None:
        self._sftp.mkdir(path)

    def _mkdir_tree(self, path: str) -> None:
        missing = []
        while path not in ("/", "") and _attrs(self._sftp, path) is None:
            missing.append(path)
            path = posixpath.dirname(path)
        for m in reversed(missing):
            try:
                self._sftp.mkdir(m)
            except OSError:
                a = _attrs(self._sftp, m)
                if not (a and statmod.S_ISDIR(a.st_mode or 0)):  # concurrent mkdir is fine
                    raise

    def _usable(self, path: str) -> bool:
        """Parents are real directories and `path` is a regular file."""
        return self._check_dirs(path) and self._check_target(path) == "file"

    # Every op below runs inside _retry, so its checks are redone after a reconnect.

    # files -----------------------------------------------------------------
    def stat(self, path: str) -> RemoteStat | None:
        def op(sftp: paramiko.SFTPClient) -> RemoteStat | None:
            if not self._check_dirs(path):
                return None
            try:
                a = sftp.lstat(path)
            except FileNotFoundError:
                return None
            return _remote_stat(a) if _kind(a) == "file" else None
        return self._retry(op)

    def _listdir(self, d: str) -> list[paramiko.SFTPAttributes]:
        def op(sftp: paramiko.SFTPClient) -> list[paramiko.SFTPAttributes]:
            try:
                return sftp.listdir_attr(d)
            except FileNotFoundError:
                return []
        return self._retry(op)

    def walk(self, root: str, skip: str | None = None) -> dict[str, RemoteStat]:
        def root_ok(sftp: paramiko.SFTPClient) -> bool:
            if not self._check_dirs(root):
                return False
            if self.roots and root not in self.roots and self._lkind(root) not in (None, "dir"):
                raise UnsafePath(f"refusing to list {root}: it is not a directory")
            return True

        if not self._retry(root_ok):
            return {}
        out: dict[str, RemoteStat] = {}
        pending = [(root, "")]
        while pending:
            d, rel = pending.pop()
            try:
                entries = self._listdir(d)
            except OSError as e:
                if not rel:
                    raise ProviderError(f"cannot list {root}: {e}") from None
                # permission denied (or a server-side failure): report it, never guess
                out[rel[:-1]] = RemoteStat(0, 0, kind="unreadable")
                continue
            for a in entries:
                path = posixpath.join(d, a.filename)
                kind = _kind(a)  # READDIR attrs are lstat attrs: a symlink is reported as one
                if kind == "dir":
                    if path != skip:
                        pending.append((path, f"{rel}{a.filename}/"))
                elif kind == "file":
                    out[rel + a.filename] = _remote_stat(a)
                else:
                    out[rel + a.filename] = RemoteStat(a.st_size or 0, int(a.st_mtime or 0),
                                                       kind=kind)
        return out

    @staticmethod
    def _replace(sftp: paramiko.SFTPClient, src: str, dst: str) -> None:
        """Move src over dst. Atomic where the server supports posix-rename; otherwise dst is
        parked under a unique name and put back if the swap fails, so it is never simply lost.
        A park left by a crash here is judged by the engine, never deleted blindly."""
        try:
            sftp.posix_rename(src, dst)  # OpenSSH extension
            return
        except OSError:
            sftp.stat(src)  # src gone? then the failure was real, not "unsupported": re-raise
        parked: str | None = park_name(dst)
        try:
            sftp.rename(dst, parked)
        except FileNotFoundError:
            parked = None
        try:
            sftp.rename(src, dst)
        except OSError as e:
            if parked:
                try:
                    sftp.rename(parked, dst)
                except OSError as restore:
                    raise ProviderError(f"could not replace {dst} ({e}) nor put it back "
                                        f"({restore}); its previous content is kept at "
                                        f"{parked}") from e
            raise
        if parked:
            sftp.remove(parked)

    def upload(self, local: Path, remote: str, mode: int | None = None) -> RemoteStat:
        def op(sftp: paramiko.SFTPClient) -> RemoteStat:
            self._ensure_dirs(remote)
            self._check_target(remote)
            tmp = tmp_name(remote)
            tmp_kind = self._lkind(tmp)
            if tmp_kind == "dir":
                raise UnsafePath(f"refusing {remote}: {tmp} is a directory")
            if tmp_kind in ("symlink", "special"):
                sftp.remove(tmp)  # removes the link itself, never its target
            sftp.put(str(local), tmp, confirm=True)
            if mode is not None:
                sftp.chmod(tmp, mode)
            if not self._check_dirs(remote):  # re-check right before the swap
                raise FileNotFoundError(remote)
            self._check_target(remote)
            self._replace(sftp, tmp, remote)
            return _remote_stat(sftp.lstat(remote))
        return self._retry(op)

    def download(self, remote: str, local: Path) -> None:
        def op(sftp: paramiko.SFTPClient) -> None:
            if not self._usable(remote):
                raise FileNotFoundError(remote)
            sftp.get(remote, str(local))
        self._retry(op)

    def delete(self, path: str) -> None:
        def op(sftp: paramiko.SFTPClient) -> None:
            if not self._usable(path):
                raise FileNotFoundError(path)
            sftp.remove(path)
        self._retry(op)

    def rename(self, src: str, dst: str) -> None:
        def op(sftp: paramiko.SFTPClient) -> None:
            if not self._usable(src):
                raise FileNotFoundError(src)
            self._ensure_dirs(dst)
            if not (self._usable(src) and self._check_dirs(dst)):  # re-check both sides
                raise FileNotFoundError(src)
            sftp.rename(src, dst)  # plain SFTP rename refuses to overwrite: intended
        self._retry(op)

    def rmdir_if_empty(self, path: str) -> bool:
        def op(sftp: paramiko.SFTPClient) -> bool:
            if not self._check_dirs(path) or self._lkind(path) != "dir":
                return False
            sftp.rmdir(path)
            return True
        try:
            return self._retry(op)
        except (OSError, UnsafePath):
            return False

    def create_exclusive(self, path: str, text: str) -> bool:
        def op(sftp: paramiko.SFTPClient) -> bool:
            self._ensure_dirs(path)
            try:
                # "wx" = WRITE|CREATE|EXCL; paramiko's bare "x" opens without WRITE. O_EXCL
                # also fails on an existing symlink, without following it.
                with sftp.open(path, "wx") as f:
                    f.write(text.encode("utf-8"))
                return True
            except OSError:
                if self._check_target(path) is None:
                    raise ProviderError(f"cannot create {path}") from None
                with sftp.open(path, "r") as f:
                    existing = f.read().decode("utf-8", "replace")
                # equal content = our own earlier attempt whose reply was lost in a reconnect
                return existing == text
        return self._retry(op)

    # commands --------------------------------------------------------------
    def supports_exec(self) -> bool:
        if self._exec_ok is None:
            try:
                # timeout: on SFTP-only servers the probe can hang instead of failing
                code, out, _ = self.exec("echo shipr-ok", timeout=self.cfg.timeout)
                self._exec_ok = code == 0 and "shipr-ok" in out
            except Exception:
                self._exec_ok = False
        return self._exec_ok

    def exec(self, command: str, cwd: str | None = None,
             timeout: float | None = None) -> tuple[int, str, str]:
        if self.client is None:
            raise ProviderError("not connected")
        if cwd:
            command = f"cd {shlex.quote(cwd)} && {command}"
        _, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        return stdout.channel.recv_exit_status(), out, err

    def sha256(self, paths: list[str]) -> dict[str, str]:
        if not paths or not self.can_exec():
            return super().sha256(paths)
        out: dict[str, str] = {}
        for i in range(0, len(paths), SHA256_BATCH):
            batch = paths[i:i + SHA256_BATCH]
            _, stdout, _ = self.exec("sha256sum -- " + " ".join(map(shlex.quote, batch)))
            for line in stdout.splitlines():
                digest, _, name = line.partition("  ")
                out[name] = digest
        # names sha256sum escaped, or no sha256sum binary at all: hash by downloading
        missing = [p for p in paths if p not in out]
        if missing:
            out.update(super().sha256(missing))
        return out

    # info ------------------------------------------------------------------
    def describe(self) -> str:
        return f"sftp://{self.cfg.username}@{self.cfg.host}:{self.cfg.port}"

    def diagnostics(self) -> list[str]:
        home = self._retry(lambda sftp: sftp.normalize("."))
        return [f"host key: {self.host_fingerprint}", f"login directory: {home}"]
