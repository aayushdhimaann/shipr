"""Typed records shipr stores on the server. They are read back from the server, so they are
validated on load like any other untrusted input."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["success", "partial", "failed", "interrupted", "dry-run", "up-to-date"]
Kind = Literal["deploy", "rollback"]


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

    @property
    def changed(self) -> bool:
        return bool(self.uploaded or self.deleted)

    @property
    def completed(self) -> bool:
        """Finished its file operations (some may have failed): the server reflects it."""
        return self.status in ("success", "partial")
