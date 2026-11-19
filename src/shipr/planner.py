"""Decide what to do with every file: compare local files (L), the manifest (M) and the server (R).

See docs/PLAN.md "How shipr tracks uploaded files" for the decision table.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable
from dataclasses import dataclass, field

from .errors import UnsafePath
from .files import LocalFile, Rules
from .models import FileEntry
from .providers import RemoteStat


def check_rel(rel: str) -> None:
    """A deploy path must be plain relative POSIX: no '..', '.', '//', '\\', NUL or leading '/'."""
    if (not rel or rel.startswith("/") or "\\" in rel or "\0" in rel
            or any(p in ("", ".", "..") for p in rel.split("/"))):
        raise UnsafePath(f"refusing unsafe path {rel!r}")


def safe_join(root: str, rel: str) -> str:
    """Join a relative path onto root, refusing anything that could escape it."""
    check_rel(rel)
    full = posixpath.normpath(posixpath.join(root, rel))
    if not full.startswith(root.rstrip("/") + "/"):
        raise UnsafePath(f"refusing path outside {root}: {rel!r}")
    return full


def stat_matches(entry: FileEntry, r: RemoteStat) -> bool:
    """Has the server file kept the size/mtime shipr recorded after uploading it?"""
    return entry.size == r.size and entry.mtime == r.mtime


@dataclass
class Op:
    path: str
    reason: str
    backup: bool = False


@dataclass
class Plan:
    uploads: list[Op] = field(default_factory=list)
    deletes: list[Op] = field(default_factory=list)
    adopt: dict[str, str] = field(default_factory=dict)  # identical untracked files -> sha
    protected: list[str] = field(default_factory=list)
    unchanged: int = 0
    forget: list[str] = field(default_factory=list)  # manifest entries already gone remotely

    @property
    def empty(self) -> bool:
        return not (self.uploads or self.deletes or self.adopt or self.forget)


def make_plan(
    local: dict[str, LocalFile],
    manifest_files: dict[str, FileEntry],
    remote: dict[str, RemoteStat],
    rules: Rules,
    remote_hashes: Callable[[list[str]], dict[str, str]] | None = None,
    reserved: str | None = None,
) -> Plan:
    """`reserved` is shipr's state dir relative to the deploy root, if it lives inside it."""
    # Validate everything before deciding anything, so a bad path aborts before any change.
    for rel in (*local, *manifest_files):
        check_rel(rel)
        if reserved and (rel == reserved or rel.startswith(reserved + "/")):
            raise UnsafePath(f"{rel!r} is inside shipr's state dir ({reserved}/); "
                             "exclude it or move state_path")
    plan = Plan()
    verify: list[str] = []

    for rel in sorted(local):
        lf, m, r = local[rel], manifest_files.get(rel), remote.get(rel)
        if r is not None and rules.protected(rel):
            plan.protected.append(rel)
        elif m is not None:
            if r is None:
                plan.uploads.append(Op(rel, "missing on server"))
            elif not stat_matches(m, r):
                plan.uploads.append(Op(rel, "changed on server since last deploy", backup=True))
            elif lf.sha256 != m.sha256:
                plan.uploads.append(Op(rel, "changed"))
            else:
                plan.unchanged += 1
        elif r is None:
            plan.uploads.append(Op(rel, "new"))
        elif r.size != lf.size:
            plan.uploads.append(Op(rel, "differs from server copy", backup=True))
        else:
            verify.append(rel)  # same size, never deployed by shipr: compare contents

    if verify:
        hashes = remote_hashes(verify) if remote_hashes else {}
        for rel in verify:
            if hashes.get(rel) == local[rel].sha256:
                plan.adopt[rel] = local[rel].sha256
            else:
                plan.uploads.append(Op(rel, "differs from server copy", backup=True))
        plan.uploads.sort(key=lambda o: o.path)

    for rel in sorted(set(manifest_files) - set(local)):
        if rel not in remote:
            plan.forget.append(rel)
        elif rules.protected(rel):
            plan.protected.append(rel)
        else:
            plan.deletes.append(Op(rel, "removed locally", backup=True))
    return plan
