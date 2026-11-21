"""SFTP provider (paramiko).

Settings: host, port=22, username, password | key_file [+ passphrase] (neither: ssh-agent),
timeout=15, retries=3, known_hosts (extra file, optional), host_key (pinned fingerprint(s)).
Host keys are always verified. A pinned `host_key` is the trust anchor and wins over
known_hosts. Otherwise an unknown host is only accepted with `trust_new_host` *and* an explicit
confirmation of its fingerprint (a terminal prompt), then saved to shipr's own known_hosts.
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
import socket
import stat as statmod
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

import paramiko
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError, field_validator

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
    password: SecretStr | None = None  # SecretStr: never shown by repr() or tracebacks
    key_file: str | None = None
    passphrase: SecretStr | None = None
    timeout: float = 15
    retries: int = 3
    known_hosts: str | None = None
    # pinned server key fingerprint(s), as `ssh-keygen -lf` prints them; a list while rotating
    host_key: list[str] | None = None

    @field_validator("host_key", mode="before")
    @classmethod
    def _pins(cls, v: object) -> object:
        pins = [v] if isinstance(v, str) else v
        if pins is not None and not (isinstance(pins, list) and pins and all(
                isinstance(p, str) and _PIN.fullmatch(p) for p in pins)):
            raise ValueError("must be a fingerprint like SHA256:<43 base64 characters> (as "
                             "printed by `ssh-keygen -lf <host key>.pub`), or a list of them")
        return pins


_PIN = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")


def describe_key(key: paramiko.PKey) -> str:
    """`ssh-ed25519 SHA256:...`: the form `ssh-keygen -lf` prints, to compare by eye."""
    return f"{key.get_name()} {key.fingerprint}"  # paramiko's fingerprint includes "SHA256:"


class _HostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Decides about a host key that isn't in any known_hosts file.

    A pin (`host_key`) decides alone. Without one, the key is trusted only when `trust` was
    requested (--trust) *and* `confirm` (a terminal prompt showing the fingerprint) says yes;
    it is then saved. Refusals are ProviderErrors, so the connect loop doesn't retry them.
    """

    def __init__(self, store: Path, pins: Sequence[str], trust: bool,
                 confirm: Callable[[str, str], bool] | None):
        self.store, self.pins, self.trust, self.confirm = store, list(pins), trust, confirm

    def missing_host_key(self, client, hostname, key):
        shown = describe_key(key)
        if self.pins:
            if key.fingerprint not in self.pins:
                raise _pin_mismatch(hostname, key, self.pins)
            client.get_host_keys().add(hostname, key.get_name(), key)  # in memory only
            return
        pin_hint = (f"After verifying it with your server provider, pin it in shipr.yml with "
                    f"`host_key: {key.fingerprint}` (best for CI)")
        if not self.trust:
            raise ProviderError(f"{hostname} is not a known host (it presented {shown}). "
                                f"{pin_hint}, or run `shipr test -e <env> --trust` in a "
                                "terminal to confirm and remember it.")
        if self.confirm is None or not self.confirm(hostname, shown):
            raise ProviderError(f"{hostname}'s host key ({shown}) was not confirmed, so it is "
                                f"not trusted. {pin_hint}; `--trust` asks for confirmation in "
                                "a terminal and never trusts a key on its own.")
        client.get_host_keys().add(hostname, key.get_name(), key)
        _remember(self.store, f"{hostname} {key.get_name()} {key.get_base64()}\n")


def _pin_mismatch(hostname: str, key: paramiko.PKey, pins: Sequence[str]) -> ProviderError:
    return ProviderError(
        f"HOST KEY MISMATCH for {hostname}: the server presented {describe_key(key)}, but "
        f"shipr.yml pins {', '.join(pins)}. This can mean a man-in-the-middle attack. If the "
        "server's key was rotated, verify the new fingerprint and update `host_key`.")


def _remember(store: Path, line: str) -> None:
    """Append one known_hosts line in a single O_APPEND write (no interleaving with another
    shipr process on POSIX), skipping lines already present."""
    store.parent.mkdir(parents=True, exist_ok=True)
    existing = store.read_text(encoding="utf-8") if store.is_file() else ""
    if line.strip() in existing.splitlines():
        return
    if existing and not existing.endswith("\n"):  # hand-edited: don't glue onto its last line
        line = "\n" + line
    fd = os.open(store, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


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


_SHA256_LINE = re.compile(r"([0-9a-f]{64}) [ *](.*)")


def _sha256_line(line: str, path: str) -> str | None:
    """The digest from one `sha256sum` output line, only if it is for exactly `path`."""
    m = _SHA256_LINE.fullmatch(line)
    return m.group(1) if m and m.group(2) == path else None


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
                last = e
            except (OSError, EOFError) as e:
                last = e
            if attempt < self.cfg.retries:
                self._backoff(attempt)
        raise ProviderError(f"could not connect to {self.describe()}: {last}")

    def _connect_once(self) -> None:
        self.close()
        client = paramiko.SSHClient()
        own = app_dir() / "known_hosts"
        if not self.cfg.host_key:  # a pin is the only trust anchor: a stale entry can't veto it
            client.load_system_host_keys()
            extra = Path(self.cfg.known_hosts).expanduser() if self.cfg.known_hosts else None
            for f in (own, extra):
                if f and f.is_file():
                    client.load_host_keys(str(f))
        client.set_missing_host_key_policy(_HostKeyPolicy(
            own, self.cfg.host_key or [], self.trust_new_host, self.confirm_host_key))
        secret = lambda s: s.get_secret_value() if s else None  # noqa: E731
        try:
            client.connect(
                self.cfg.host, port=self.cfg.port, username=self.cfg.username,
                password=secret(self.cfg.password),
                key_filename=str(Path(self.cfg.key_file).expanduser())
                if self.cfg.key_file else None,
                passphrase=secret(self.cfg.passphrase), timeout=self.cfg.timeout,
                banner_timeout=self.cfg.timeout, auth_timeout=self.cfg.timeout,
                allow_agent=not secret(self.cfg.password), look_for_keys=False,
            )
            key = client.get_transport().get_remote_server_key()
            self._check_pin(key)  # a pin wins even when known_hosts accepted the key
        except BaseException:
            client.close()
            raise
        self.host_fingerprint = describe_key(key)
        self.client, self._sftp = client, client.open_sftp()
        self._sftp.get_channel().settimeout(max(self.cfg.timeout, 30))

    def _check_pin(self, key: paramiko.PKey) -> None:
        if self.cfg.host_key and key.fingerprint not in self.cfg.host_key:
            raise _pin_mismatch(self.cfg.host, key, self.cfg.host_key)

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
        """Hash with `sha256sum` over SSH where possible, else by downloading. The output is
        trusted only when it is exactly one `<64 hex>  <path>` line per requested path, in
        order, with exit status 0: a crafted file name (BusyBox doesn't escape newlines), an
        unreadable file or a missing binary all make the batch fall back to downloading."""
        if not paths or not self.can_exec():
            return super().sha256(paths)
        out: dict[str, str] = {}
        plain = [p for p in paths if "\n" not in p and "\\" not in p]  # never escaped
        for i in range(0, len(plain), SHA256_BATCH):
            batch = plain[i:i + SHA256_BATCH]
            code, stdout, _ = self.exec("sha256sum -- " + " ".join(map(shlex.quote, batch)))
            lines = stdout.splitlines()
            if code != 0 or len(lines) != len(batch):
                continue
            digests = [_sha256_line(line, path) for line, path in zip(lines, batch, strict=True)]
            if all(digests):
                out.update(zip(batch, digests, strict=True))
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
