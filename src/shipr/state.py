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
    def __init__(self, provider: Provider, state_path: str, inside_webroot: bool):
        self.provider = provider
        self.root = state_path
        self.inside_webroot = inside_webroot

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
        for name in sorted(self.provider.walk(self._path("history"))):
            if not name.endswith(".json"):
                continue
            text = self.provider.read_text(self._path("history", name))
            try:
                out.append(DeployRecord.model_validate_json(text or ""))
            except ValidationError:
                continue  # one unreadable record must not hide the rest
        return out

    def backup_path(self, deploy_id: str, rel: str) -> str:
        return self._path("backups", deploy_id, rel)
