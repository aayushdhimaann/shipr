"""shipr's records on the server, under the environment's state dir:

    manifest.json            what is deployed (models.Manifest)
    lock                     held for the length of a deploy
    history/<deploy-id>.json one models.DeployRecord per deploy
    backups/<deploy-id>/...  server copies saved before overwrite/delete
"""

from __future__ import annotations

import getpass
import json
import posixpath
import secrets
import socket
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import ValidationError

from .errors import LockedError, StateError
from .models import DeployRecord, Manifest
from .providers import Provider
from .providers.base import is_within

HTACCESS = "# written by shipr: keep deploy records private\nRequire all denied\nDeny from all\n"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_deploy_id(sha: str) -> str:
    t = datetime.now(timezone.utc)
    # milliseconds keep ids (and so history order) unique for back-to-back deploys
    return t.strftime("%Y%m%dT%H%M%S") + f"{t.microsecond // 1000:03d}Z-{sha[:7]}"


def whoami() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # no login name (some containers / CI)
        user = "unknown"
    return f"{user}@{socket.gethostname()}"


@dataclass(frozen=True)
class LockInfo:
    """What a lock file says about its holder (None: not a lock shipr can identify)."""

    deploy_id: str | None = None
    by: str | None = None
    since: str | None = None

    @classmethod
    def parse(cls, text: str) -> LockInfo:
        try:
            data = json.loads(text)
        except ValueError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        field = lambda k: data[k] if isinstance(data.get(k), str) else None  # noqa: E731
        return cls(field("deploy_id"), field("by"), field("since"))

    def describe(self, now: str | None = None) -> str:
        if not self.deploy_id:
            return ("an unrecognised lock file" + (f" written by {self.by}" if self.by else "")
                    + " (shipr can't tell which deploy wrote it)")
        age = _age(self.since, now or now_iso())
        return (f"deploy {self.deploy_id} by {self.by or 'unknown'}, since {self.since or '?'}"
                + (f" ({age} ago)" if age else ""))


def _age(since: str | None, now: str) -> str | None:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        secs = int((datetime.strptime(now, fmt) - datetime.strptime(since or "", fmt))
                   .total_seconds())
    except ValueError:
        return None
    if secs < 0:
        return None
    d, h, m = secs // 86400, secs // 3600 % 24, secs // 60 % 60
    return (f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m" if m else f"{secs}s")


class RemoteState:
    def __init__(self, provider: Provider, state_path: str, remote_path: str,
                 legacy: bool = False, env_name: str = "<env>"):
        self.provider = provider
        self.root = state_path
        self.remote_path = remote_path
        self.legacy = legacy  # found at shipr 0.1's default location, not configured
        self.env_name = env_name  # for the exact `shipr unlock` command in messages
        self._lock_text: str | None = None  # the lock this object created, byte for byte
        # relative to remote_path when the records sit inside the deploy folder, else None
        self.rel = state_path[len(remote_path) + 1:] if is_within(state_path, remote_path) \
            and state_path != remote_path else None

    @property
    def inside_webroot(self) -> bool:
        return self.rel is not None

    def _path(self, *parts: str) -> str:
        return posixpath.join(self.root, *parts)

    # manifest --------------------------------------------------------------
    def load_manifest(self) -> Manifest:
        path = self._path("manifest.json")
        text = self.provider.read_text(path)
        if text is None:
            return Manifest()
        try:
            return Manifest.model_validate_json(text)
        except ValidationError as e:
            raise StateError(f"{path} is corrupt ({e.error_count()} problems); "
                             "fix or remove it, then deploy again") from None

    def check_owner(self, manifest: Manifest, project: str) -> list[str]:
        """Refuse records that belong to another deploy target (two environments sharing a
        state dir); return warnings for softer mismatches."""
        if manifest.remote_path and manifest.remote_path != self.remote_path:
            raise StateError(
                f"the deploy records in {self.root} belong to remote_path "
                f"{manifest.remote_path} (environment {manifest.environment!r}), not "
                f"{self.remote_path}: two environments share a state directory. Give each "
                "environment its own state_path.")
        if manifest.project and manifest.project != project:
            return [f"the deploy records in {self.root} were written for project "
                    f"{manifest.project!r}, not {project!r} (renamed project?)"]
        return []

    def save_manifest(self, manifest: Manifest) -> None:
        self.provider.write_text(self._path("manifest.json"),
                                 manifest.model_dump_json(indent=1, exclude_none=True))

    def ensure_private(self) -> None:
        """Block web access to the state dir when it lives in the web root (Apache)."""
        if self.inside_webroot and self.provider.stat(self._path(".htaccess")) is None:
            self.provider.write_text(self._path(".htaccess"), HTACCESS)

    # lock ------------------------------------------------------------------
    # SFTP has no compare-and-delete. A lock is identified by its exact content, which holds a
    # random token, and is only ever removed after its content was checked right before the
    # removal (Provider.delete_if_content). That leaves one request's window in which a lock
    # removed by `shipr unlock` and re-taken by another deploy could be deleted; see
    # docs/security.md. Locks never expire: a stale one is removed by a person (`shipr unlock`).
    def acquire_lock(self, deploy_id: str) -> None:
        text = json.dumps({"by": whoami(), "since": now_iso(), "deploy_id": deploy_id,
                           "token": secrets.token_hex(8)})
        if not self.provider.create_exclusive(self._path("lock"), text):
            raise LockedError(self.held_message(self.read_lock()))
        self._lock_text = text

    def held_message(self, held: str | None) -> str:
        if held is None:  # released between our attempt and this read
            return "another deploy held the lock and has just released it; deploy again."
        info = LockInfo.parse(held)
        fix = (f"`shipr unlock -e {self.env_name} --id {info.deploy_id}`" if info.deploy_id
               else f"`shipr unlock -e {self.env_name}` in a terminal")
        return (f"another deploy holds the lock: {info.describe()}.\nWait for it to finish. If "
                f"it is no longer running (killed, or it lost its connection), remove the lock "
                f"with {fix}.")

    def read_lock(self) -> str | None:
        return self.provider.read_text(self._path("lock"))

    def lock_owned(self) -> bool:
        """Is the lock still exactly the one this object created?"""
        return self._lock_text is not None and self.read_lock() == self._lock_text

    def release_lock(self) -> str:
        """Remove this object's own lock, never another's: "deleted"; "missing" (someone
        removed it); "changed" (it is now another deploy's; left in place); "not-held"."""
        if self._lock_text is None:
            return "not-held"
        outcome = self.remove_lock(self._lock_text)
        if outcome != "changed":
            self._lock_text = None
        return outcome

    def remove_lock(self, seen: str) -> str:
        """Remove the lock only if it is still exactly `seen` (`shipr unlock`, after a person
        confirmed that holder): "deleted", "missing" or "changed"."""
        return self.provider.delete_if_content(self._path("lock"), seen)

    # history ---------------------------------------------------------------
    def save_history(self, record: DeployRecord) -> None:
        self.provider.write_text(self._path("history", f"{record.id}.json"),
                                 record.model_dump_json(indent=1))

    def list_history(self) -> list[DeployRecord]:
        """Oldest first (deploy ids start with a UTC timestamp)."""
        out = []
        for name, r in sorted(self.provider.walk(self._path("history")).items()):
            if not name.endswith(".json") or r.kind != "file":
                continue
            text = self.provider.read_text(self._path("history", name))
            try:
                out.append(DeployRecord.model_validate_json(text or ""))
            except ValidationError:
                continue  # one unreadable record must not hide the rest
        return out

    def backup_path(self, deploy_id: str, rel: str) -> str:
        return self._path("backups", deploy_id, rel)

    def backup_copies(self, rels: set[str]) -> dict[str, list[str]]:
        """Every backup of each deploy-relative path, across all deploys (absolute paths)."""
        out: dict[str, list[str]] = {}
        for name, r in self.provider.walk(self._path("backups")).items():
            _, _, rel = name.partition("/")  # <deploy-id>/<rel>
            if rel in rels and r.kind == "file":
                out.setdefault(rel, []).append(self._path("backups", name))
        return out
