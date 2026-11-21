"""Typed records shipr stores on the server. They are read back from the server, so they are
validated on load like any other untrusted input."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["success", "partial", "failed", "interrupted", "dry-run", "up-to-date"]
Kind = Literal["deploy", "rollback"]
# remote hooks of a deploy: none configured, all ran, skipped by ssh_exec policy / no shell,
# one failed, or not run (deploy stopped first, or files failed)
HookState = Literal["none", "ok", "skipped", "failed", "not-run"]


class _Record(BaseModel):
    model_config = ConfigDict(extra="ignore")  # tolerate fields from newer shipr versions


class FileEntry(_Record):
    sha256: str
    size: int
    mtime: int


class InProgress(_Record):
    deploy_id: str
    by: str
    started: str


class Manifest(_Record):
    """What shipr deployed: one entry per file it put on the server."""

    version: int = 1
    files: dict[str, FileEntry] = Field(default_factory=dict)
    environment: str | None = None
    deployed_commit: str | None = None
    branch: str | None = None
    deploy_id: str | None = None
    deployed_at: str | None = None
    in_progress: InProgress | None = None
    # which deploy target these records belong to (absent in manifests from shipr 0.1)
    project: str | None = None
    remote_path: str | None = None
    # files of deployed_commit are on the server but its remote hooks haven't all run yet
    hooks_pending: bool = False


class UploadedFile(_Record):
    path: str
    reason: str
    backup: str | None = None


class DeletedFile(_Record):
    path: str
    backup: str


class FailedFile(_Record):
    path: str
    error: str


class DeployRecord(_Record):
    """One entry of `shipr history`."""

    id: str
    kind: Kind = "deploy"
    project: str
    environment: str
    commit: str
    branch: str | None = None
    subject: str = ""
    by: str
    target: str
    started: str
    finished: str | None = None
    duration_s: float = 0.0
    status: Status = "failed"
    error: str | None = None
    uploaded: list[UploadedFile] = Field(default_factory=list)
    deleted: list[DeletedFile] = Field(default_factory=list)
    failed: list[FailedFile] = Field(default_factory=list)
    kept: list[str] = Field(default_factory=list)
    # left untouched: at or under a server symlink, special file or unreadable directory
    blocked: list[str] = Field(default_factory=list)
    # shipr's own leftovers from interrupted uploads that were verified and removed
    cleaned: list[str] = Field(default_factory=list)
    # stages, so `status` never hides which part of a deploy did or didn't happen
    files_done: bool = False  # every planned file operation was attempted (some may have failed)
    hooks: HookState = "none"
    manifest_saved: bool = False  # (history itself: a deploy whose history failed exits non-zero)

    @property
    def changed(self) -> bool:
        return bool(self.uploaded or self.deleted)

    @property
    def completed(self) -> bool:
        """Finished its file operations (some may have failed): the server reflects it, even
        if a hook failed afterwards. Records from shipr 0.1 have no `files_done`."""
        return self.files_done or self.status in ("success", "partial")
