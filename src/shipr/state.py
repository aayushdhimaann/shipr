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
import socket
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


class RemoteState:
    def __init__(self, provider: Provider, state_path: str, remote_path: str,
                 legacy: bool = False):
        self.provider = provider
        self.root = state_path
        self.remote_path = remote_path
        self.legacy = legacy  # found at shipr 0.1's default location, not configured
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
    def acquire_lock(self, deploy_id: str) -> None:
        info = json.dumps({"by": whoami(), "since": now_iso(), "deploy_id": deploy_id})
        if not self.provider.create_exclusive(self._path("lock"), info):
            raise LockedError(
                f"another deploy holds the lock: {self.read_lock() or '(unreadable)'}\n"
                "If that deploy is dead, run `shipr unlock -e <env>`.")

    def read_lock(self) -> str | None:
        return self.provider.read_text(self._path("lock"))

    def release_lock(self, deploy_id: str | None = None) -> None:
        """Remove the lock; with deploy_id, only if this deploy still owns it (someone may
        have run `shipr unlock` and started another deploy meanwhile)."""
        held = self.read_lock()
        if held is None or (deploy_id and deploy_id not in held):
            return
        self.provider.delete(self._path("lock"))

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
