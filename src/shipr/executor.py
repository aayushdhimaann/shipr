"""Apply a Plan to the server, keeping the manifest and the deploy record in step with every
file that actually changed, so an interruption at any point leaves accurate records."""

from __future__ import annotations

import posixpath

from .errors import ConnectionLost, ProviderError, UnsafePath
from .files import LocalFile
from .models import DeletedFile, DeployRecord, FailedFile, FileEntry, Manifest, UploadedFile
from .planner import Op, Plan, safe_join
from .providers import Provider, RemoteStat
from .state import RemoteState
from .ui import UI

CHECKPOINT_EVERY = 25
# Per-file problems (permission denied, disk full, a symlink that appeared on the server after
# planning) are recorded and the deploy continues; ConnectionLost stops it.
FILE_ERRORS = (OSError, ProviderError, UnsafePath)


class PlanExecutor:
    def __init__(self, provider: Provider, state: RemoteState, manifest: Manifest,
                 record: DeployRecord, local: dict[str, LocalFile],
                 remote: dict[str, RemoteStat], remote_path: str, ui: UI):
        self.provider = provider
        self.state = state
        self.manifest = manifest
        self.record = record
        self.local = local
        self.remote = remote
        self.root = remote_path
        self.ui = ui
        self._ops_done = 0

    def run(self, plan: Plan, deletes: list[Op]) -> None:
        self._apply_bookkeeping(plan)
        for op in plan.uploads:
            self._guarded(op, self._upload)
        for op in deletes:
            self._guarded(op, self._delete)

    def _apply_bookkeeping(self, plan: Plan) -> None:
        """Record identical untracked files; drop entries for files already gone remotely."""
        for rel, sha in plan.adopt.items():
            r = self.remote[rel]
            self.manifest.files[rel] = FileEntry(sha256=sha, size=r.size, mtime=r.mtime)
        for rel in plan.forget:
            self.manifest.files.pop(rel, None)

    def _guarded(self, op: Op, action) -> None:
        try:
            action(op)
        except ConnectionLost:
            raise
        except FILE_ERRORS as e:
            self.record.failed.append(FailedFile(path=op.path, error=str(e)))
        self._checkpoint()

    def _upload(self, op: Op) -> None:
        target = safe_join(self.root, op.path)
        existing = self.remote.get(op.path)
        backup = None
        if op.backup and existing:
            backup = self.state.backup_path(self.record.id, op.path)
            self.provider.copy(target, backup)
        lf = self.local[op.path]
        # replacing: keep the server's permissions; new: use the local mode (keeps +x)
        mode = existing.mode if existing and existing.mode is not None else lf.mode
        rs = self.provider.upload(lf.path, target, mode)
        self.manifest.files[op.path] = FileEntry(sha256=lf.sha256, size=rs.size, mtime=rs.mtime)
        self.record.uploaded.append(UploadedFile(path=op.path, reason=op.reason, backup=backup))
        self.ui.echo(f"  ↑ {op.path}")

    def _delete(self, op: Op) -> None:
        target = safe_join(self.root, op.path)
        backup = self.state.backup_path(self.record.id, op.path)
        self.provider.rename(target, backup)  # "delete" = move into the backup folder
        self.manifest.files.pop(op.path, None)
        self.record.deleted.append(DeletedFile(path=op.path, backup=backup))
        self.ui.echo(f"  ✗ {op.path}")
        self._remove_empty_parents(target)

    def _remove_empty_parents(self, path: str) -> None:
        d = posixpath.dirname(path)
        while d != self.root and d.startswith(self.root + "/") and self.provider.rmdir_if_empty(d):
            d = posixpath.dirname(d)

    def _checkpoint(self) -> None:
        self._ops_done += 1
        if self._ops_done % CHECKPOINT_EVERY == 0:
            self.state.save_manifest(self.manifest)
